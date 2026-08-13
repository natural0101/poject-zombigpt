"""The sidecar's attach / observe / act loop.

Nothing here decides anything about the game. Every judgement the loop makes is
made by a subsystem that was written and tested for it: the session rules by
:class:`~pz_agent_core.session.handshake.SessionManager`, liveness by
:class:`~pz_agent_core.session.heartbeat.HeartbeatMonitor`, sequencing and
idempotency by :class:`~pz_agent_core.ipc.queue.CommandQueue`, the world model by
:class:`~pz_agent_core.observation.store.ObservationStore`, safety by
:class:`~pz_agent_core.safety.reflex.ReflexGuard`, and one command's lifecycle by
:class:`~pz_agent_core.actions.engine.ActionEngine`. This module is wiring and
lifecycle, and the five properties below are what it is responsible for:

* **Attaching is not arming, and arming is the game's to confirm.**
  :meth:`SidecarLoop.attach` always comes up in ``OBSERVE``, on a fresh session
  and on a resumed one alike, and there is no parameter that changes that.
  ``session.default_mode`` in ``config.toml`` names what :meth:`SidecarLoop.arm`
  would select; it is not consulted here. The only code that sets ``armed`` is
  the arm's confirmation (:meth:`SidecarLoop._grant_arm`), reached exclusively
  through an explicit :meth:`SidecarLoop.arm` — which submits a ``session.arm``
  command to the mod and grants nothing until the mod's terminal ack *and* a
  game heartbeat reporting the armed mode are both observed, because the
  2026-08-08 live run proved a sidecar that flips its own flag arms nobody.
  An arm request issued before this process attached is additionally refused,
  so a request file left behind by a crashed run cannot re-arm the run that
  replaces it.

* **The reflex guard runs first.** Every tick evaluates it before a single
  command is composed, whether or not a planner is attached and whether or not
  anything is armed. §7.1 puts reflexes below the planner precisely so they keep
  working when there is no planner, and an ordering that ran them afterwards
  would only satisfy that on the ticks where nothing happened.

* **A silent game closes in-flight work as lost.** A missing or stale game
  heartbeat is ``GAME_DISCONNECTED``, and whatever the mod was running is
  terminated as ``lost`` — never ``failed`` and never ``succeeded``, because
  nobody observed which it was.

* **``panic.stop`` is obeyed as a level, not an edge.** While the sentinel is in
  the exchange directory the loop disarms, closes in-flight work and starts
  nothing, on that tick and on every subsequent one. Clearing it does not re-arm
  anything; only the user does.

* **Everything is bounded.** A tick budget on :meth:`SidecarLoop.run`, a rate cap
  on actions started per window, a bounded observation ring, a bounded number of
  retained safety events, and a bounded number of observation records ingested
  per tick. Nothing here sleeps except through the injected sleeper.

* **A goal is served by the queue's rules or not at all.** When the loop holds a
  :class:`~pz_agent_core.goals.GoalQueue`, every transition a goal makes goes
  through that queue — activation only armed in ``AUTONOMOUS`` with a
  :class:`GoalPlanner`, one step charged per action outcome, success only on the
  engine's own observed evidence, and the stop levers mapped one-to-one: a
  disarm (the user's, the guard's, a silent game's) ends the active goal through
  :meth:`~pz_agent_core.goals.GoalQueue.disarm`, a panic sentinel empties the
  whole channel through :meth:`~pz_agent_core.goals.GoalQueue.panic_stop`. Two
  threads reach the queue — this loop's tick and the Core RPC serving thread —
  and both do so only under :attr:`SidecarLoop.goal_lock`; see that field.

* **A remote action is dispatched by this loop's tick or not at all.** The Core
  RPC serving thread never touches the engine: ``action.submit`` puts a request
  into the loop's :class:`ActionChannel` — a bounded queue whose admission
  refuses when full, never drops — and the tick thread drains at most one
  submission per tick through the *same* funnel a planner proposal takes: the
  same arming gate, the same action budget, the same
  :meth:`~pz_agent_core.actions.engine.ActionEngine.execute` with its
  capability check and permission machinery, so every dispatch happens on the
  tick thread exactly as a local one does. Every state a submission passes
  through is a frozen :class:`~pz_agent_mcp.ports.ActionRecord` replaced whole,
  and the stop levers map the same way the goal channel's do: a disarm ends
  what is still waiting as ``NOT_ARMED``, a panic sentinel as ``PANIC_STOP``,
  a shutdown as ``CANCELLED_BY_REQUEST`` — a terminal record that says why,
  never a submission that silently vanishes.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, TypeAlias, runtime_checkable

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.engine import (
    ANY_SEQ,
    ActionEngine,
    ActionRequest,
    CapabilityCheck,
    Dispatch,
    deny_capability,
)
from pz_agent_core.capabilities import (
    MAX_NOTES,
    Capability,
    CapabilityError,
    CapabilityReport,
    ReportIOError,
    ScanError,
    confirm,
    probe_for,
    save_report,
)
from pz_agent_core.diagnostics import DiagnosticsError, TraceError, TraceWriter
from pz_agent_core.goals import (
    GoalDocumentError,
    GoalQueue,
    GoalRecord,
    GoalState,
    GoalStore,
    GoalStoreError,
    to_planner_goal,
)
from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.ipc.journal import JournalReader, probe_truncation
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.ipc.snapshot import SnapshotMiss, SnapshotReader
from pz_agent_core.observation.diff import DiffError
from pz_agent_core.observation.store import DEFAULT_WINDOW, ObservationStore
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CapabilityState,
    Command,
    JsonDict,
    Observation,
    ProtocolError,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.safety.reflex import (
    InFlightCommand,
    ReflexGuard,
    ReflexSignals,
    SafetyEvent,
)
from pz_agent_core.session.handshake import SessionDescriptor, SessionManager
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.session.lock import SidecarLock
from pz_agent_core.version import PRODUCT_VERSION

# The one boundary type this module borrows from the MCP package, and it is a
# record rather than machinery: `ActionRecord` carries the project's honesty
# rule in its own `__post_init__` (succeeded requires the terminal result and
# its observed postcondition), and restating that shape here would be a second
# copy of the rule — the copy that drifts is always the one on the side that
# reports success. The dependency is one-way and cycle-free: `pz_agent_mcp`
# never imports this package at module scope.
from pz_agent_mcp.ports import ActionRecord

from .supervisor import (
    CONTROL_MAX_AGE_MS,
    ControlChannel,
    ControlKind,
    ControlRequest,
    PidFile,
    # The state directory's whole-document read and write. Borrowed rather than
    # repeated: the supervisor's files and this module's capability record share
    # one directory, and two independent scratch-and-replace implementations in
    # one directory is how one of them starts reading the other's temp file.
    _read_json,
    _write_json,
)

#: Wall time between ticks. Half the mod's 4 Hz observation cadence, so a tick
#: never has to wait for the next frame to have something to read.
DEFAULT_TICK_INTERVAL_MS: Final = 125

MAX_TICK_INTERVAL_MS: Final = 5_000

#: Ticks one :meth:`SidecarLoop.run` will take before it returns. At the default
#: interval this is eight hours — longer than a session, short enough that a
#: forgotten sidecar is not a process that runs until the machine is rebooted.
DEFAULT_TICK_BUDGET: Final = 230_400

#: Hard ceiling on the tick budget, so a configured one cannot make the loop the
#: unbounded thing this project forbids.
MAX_TICK_BUDGET: Final = 5_000_000

#: Dispatched commands held for pairing with their terminal result in the trace.
#: One is the working set — the engine drives each command to a terminal result
#: before returning — and the rest is slack for results that never arrive.
MAX_TRACED_COMMANDS: Final = 8

#: Window the action rate cap is measured over.
DEFAULT_ACTION_WINDOW_MS: Final = 60_000

#: Actions the loop may *start* per window. Well above what a planner issuing one
#: step at a time can reach, and far below the rate at which a stuck plan would
#: hammer the mod's queue.
DEFAULT_MAX_ACTIONS_PER_WINDOW: Final = 12

MAX_ACTIONS_PER_WINDOW: Final = 240

#: Observation records taken off the journal in one tick.
DEFAULT_OBSERVATIONS_PER_TICK: Final = 64

MAX_OBSERVATIONS_PER_TICK: Final = 1_024

#: Safety events kept for diagnostics. A ring, not a log.
MAX_RETAINED_EVENTS: Final = 32

#: Polls :class:`JournalObservationSource` will make while the engine waits for a
#: fresh observation. The deadline alone is not enough: the clock is injected, and
#: a clock that does not move must not become a spin.
MAX_SOURCE_POLLS: Final = 2_000

#: Actions whose failure to move the character is what ``PATH_STUCK`` means.
_MOVING_ACTIONS: Final[frozenset[ActionName]] = frozenset(
    {ActionName.MOVEMENT_MOVE_TO, ActionName.MOVEMENT_MOVE_NEAR}
)

#: Modes :meth:`SidecarLoop.arm` accepts. ``OBSERVE`` and ``OFF`` are what
#: disarming means, and the two remaining ones are not this build's to grant:
#: ``REFLEX_ONLY`` is what the loop already does unarmed, and
#: ``EXPERIMENTAL_INPUT`` drives synthetic input, which nothing here implements.
ARMABLE_MODES: Final[frozenset[SessionMode]] = frozenset(
    {SessionMode.ASSISTED, SessionMode.AUTONOMOUS}
)

#: Wall time an arm may wait for the game's own confirmation before it is
#: refused. Live finding (Build 42.20.2, 2026-08-08): ``pz_session_arm``
#: answered ``armed=true`` having armed only the sidecar — no ``session.arm``
#: command was ever enqueued, and the game kept publishing ``armed=false`` /
#: ``mode=OFF``. Arming is therefore two-phase: the command travels the queue,
#: and authority is granted only once the mod's terminal ack *and* a game
#: heartbeat reporting the armed mode are both observed. Forty ticks at the
#: default interval — several round trips for a mod that answers, short enough
#: that a mod that does not answer is a refusal, not a hang.
DEFAULT_ARM_CONFIRM_TIMEOUT_MS: Final = 5_000

#: Hard ceiling on the configured confirmation window; it doubles as the lease
#: on the ``session.arm`` command itself, so it must stay a lease the protocol
#: accepts (``MAX_LEASE_MS``) and short enough that "the game never confirmed"
#: arrives while the user is still watching.
MAX_ARM_CONFIRM_TIMEOUT_MS: Final = 60_000

#: Sleeps for the given number of milliseconds. Injected everywhere so a test
#: drives thousands of ticks without a single real pause.
Sleeper: TypeAlias = Callable[[int], None]


def system_sleep_ms(milliseconds: int) -> None:
    """Default :data:`Sleeper`."""
    if milliseconds > 0:
        time.sleep(milliseconds / 1000.0)


class LoopError(ValueError):
    """The loop was asked to do something in a state where it cannot."""


class Planner(Protocol):
    """The optional source of intent.

    Deliberately the narrowest possible port: one observation in, at most one
    :class:`~pz_agent_core.actions.engine.ActionRequest` out. The loop runs
    identically with no planner at all, which is the property §7.1 requires of
    ``provider = "none"``.
    """

    def propose(self, observation: Observation) -> ActionRequest | None:
        """The next action to attempt, or None when there is nothing to do."""
        ...


@runtime_checkable
class GoalPlanner(Planner, Protocol):
    """A planner that can also serve one explicitly stated goal.

    The typed goal channel needs a planner the loop can hand *the* goal to —
    the plan asked for must be the goal the queue activated, not whatever the
    planner's own initiative would have picked. The goal crosses this seam as
    the planner package's :class:`~pz_agent_core.planner.Goal`, widened by
    :func:`~pz_agent_core.goals.to_planner_goal` from the record the queue
    holds, which is the same one-line bridge the core-side contract test
    (``tests/contract/test_goal_reaches_the_planner.py``) proves total.

    ``runtime_checkable`` because the loop must not activate a goal its planner
    has no way to serve: :meth:`SidecarLoop._act` checks this protocol before
    it activates anything, so a loop assembled with a plain :class:`Planner`
    leaves admitted goals pending until their own time-to-live expires them —
    a bounded, reported end rather than a silent drop.
    """

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        """The next action in service of *goal*, or None when nothing serves it now."""
        ...


@runtime_checkable
class ContainerMemory(Protocol):
    """The optional feed from what the loop observes into the save's memory.

    :class:`~pz_agent_cli.memory.SidecarMemory` is the one implementation, and
    it lives behind the planner (``planner.memory``) rather than on this loop,
    because ``build_loop`` hands it to the planner and nothing else holds it.
    This protocol is the loop's structural view of it: the planner and wrapper
    classes import this module, so this module cannot import them back and
    name the concrete types — the same one-way rule the :class:`Planner`
    protocol already applies one level up.

    ``runtime_checkable`` so :meth:`SidecarLoop._container_memory` can tell the
    feedable memory apart from the fallbacks (``NoMemory``,
    ``UnreadableMemory``) that answer the autonomy questions but store nothing.

    All three methods are bounded and never raise: a memory that cannot record
    or flush reports through its own record, and remembering shelves is never a
    reason to end the tick that was only walking past them.
    """

    def note_observation(self, observation: Observation, *, now_ms: int) -> int:
        """Record a sighting for each world container the observation shows."""
        ...

    def note_inspection(self, container_ref: str, observation: Observation, *, now_ms: int) -> bool:
        """Record that this container's contents were just enumerated."""
        ...

    def flush_notes(self, *, now_ms: int) -> bool:
        """Write pending notes now; the shutdown path."""
        ...


#: Evidence kinds that prove a container's contents were just observed, and the
#: key inside the evidence's ``observed`` block that names that container.
#: ``container.inspect`` describes contents outright; the two transfer kinds
#: prove items were observed landing in the destination, after which the
#: destination's enumeration in the same observation is current. The batch kind
#: names its container ``destination_ref`` by the transfer_batch contract; the
#: other two share the single-container spelling.
_CONTENT_EVIDENCE_REFS: Final[Mapping[str, str]] = {
    "container_contents_described": "container_ref",
    "item_in_destination_container": "container_ref",
    "items_in_destination_container": "destination_ref",
}

#: Wrapper hops :meth:`SidecarLoop._container_memory` will look through. The
#: shipped assembly is exactly one wrapper deep (NavigatingPlanner around
#: AutonomyPlanner); the slack is for a second wrapper, not for a chain.
_MAX_PLANNER_UNWRAPS: Final = 4


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Every bound the loop runs under. All of them are enforced, not advisory."""

    tick_interval_ms: int = DEFAULT_TICK_INTERVAL_MS
    tick_budget: int = DEFAULT_TICK_BUDGET
    max_actions_per_window: int = DEFAULT_MAX_ACTIONS_PER_WINDOW
    action_window_ms: int = DEFAULT_ACTION_WINDOW_MS
    observations_per_tick: int = DEFAULT_OBSERVATIONS_PER_TICK
    observation_window: int = DEFAULT_WINDOW
    arm_confirm_timeout_ms: int = DEFAULT_ARM_CONFIRM_TIMEOUT_MS

    def __post_init__(self) -> None:
        if not 0 <= self.tick_interval_ms <= MAX_TICK_INTERVAL_MS:
            raise LoopError(
                f"tick_interval_ms must be within 0..{MAX_TICK_INTERVAL_MS}, "
                f"got {self.tick_interval_ms}"
            )
        if not 1 <= self.tick_budget <= MAX_TICK_BUDGET:
            raise LoopError(
                f"tick_budget must be within 1..{MAX_TICK_BUDGET}, got {self.tick_budget}"
            )
        if not 1 <= self.max_actions_per_window <= MAX_ACTIONS_PER_WINDOW:
            raise LoopError(
                f"max_actions_per_window must be within 1..{MAX_ACTIONS_PER_WINDOW}, "
                f"got {self.max_actions_per_window}"
            )
        if self.action_window_ms <= 0:
            raise LoopError(f"action_window_ms must be positive, got {self.action_window_ms}")
        if not 1 <= self.observations_per_tick <= MAX_OBSERVATIONS_PER_TICK:
            raise LoopError(
                f"observations_per_tick must be within 1..{MAX_OBSERVATIONS_PER_TICK}, "
                f"got {self.observations_per_tick}"
            )
        # The floor is the protocol's own MIN_LEASE_MS (100): the window is also
        # the lease on the session.arm command, and a lease the protocol refuses
        # would turn every arm into an INVALID_ARGUMENT at build time.
        if not 100 <= self.arm_confirm_timeout_ms <= MAX_ARM_CONFIRM_TIMEOUT_MS:
            raise LoopError(
                f"arm_confirm_timeout_ms must be within 100..{MAX_ARM_CONFIRM_TIMEOUT_MS}, "
                f"got {self.arm_confirm_timeout_ms}"
            )


DEFAULT_LIMITS: Final = LoopLimits()


class ActionBudget:
    """A sliding-window cap on how many actions may be *started*.

    Started rather than completed: an action that never finishes still occupied
    the mod's queue, and a rate limit that only counted completions would let a
    plan that fails instantly retry without bound.
    """

    def __init__(self, limit: int, window_ms: int) -> None:
        if limit < 1:
            raise LoopError(f"limit must be positive, got {limit}")
        if window_ms <= 0:
            raise LoopError(f"window_ms must be positive, got {window_ms}")
        self._limit = limit
        self._window_ms = window_ms
        self._stamps: deque[int] = deque(maxlen=limit)

    @property
    def limit(self) -> int:
        return self._limit

    def _expire(self, now_ms: int) -> None:
        while self._stamps and now_ms - self._stamps[0] >= self._window_ms:
            self._stamps.popleft()

    def allows(self, now_ms: int) -> bool:
        self._expire(now_ms)
        return len(self._stamps) < self._limit

    def spend(self, now_ms: int) -> None:
        """Record one started action.

        Raises:
            LoopError: when the budget is exhausted. The caller is expected to
                have asked :meth:`allows` first; spending past the cap would make
                the bound advisory, which is the one thing it must not be.
        """
        self._expire(now_ms)
        if len(self._stamps) >= self._limit:
            raise LoopError(f"the action budget of {self._limit} per window is spent")
        self._stamps.append(now_ms)

    def spent(self, now_ms: int) -> int:
        self._expire(now_ms)
        return len(self._stamps)


# ---------------------------------------------------------------------------
# ports the engine plugs into
# ---------------------------------------------------------------------------


@dataclass
class ObservationFeed:
    """Turns the exchange directory's two observation channels into messages.

    The journal is the ordinary path and the snapshot is the recovery path: when
    the store says it has no baseline it can build on — first attach, a sequence
    gap, a session change — the pointer is followed and the full document is
    offered instead. Anything that does not parse is counted and skipped, because
    one unusable record must not stall a stream the whole loop reads.

    The recovery path is bound to one session (see
    :attr:`~pz_agent_core.ipc.snapshot.SnapshotReader.session_id`, wired at
    :meth:`SidecarLoop._build`). A document left in the slots by the session
    before this one is refused rather than offered, and the refusal is a miss
    whose reason lands in :attr:`diagnostics`: the loop then keeps reporting no
    observation — ``ingested`` stays 0 and the store keeps asking for a full
    snapshot — until the mod publishes under the live session, which is the
    honest picture. The alternative was a baseline describing a world that had
    ended, and every reflex evaluation and arm decision made against it.
    """

    layout: IpcLayout
    reader: JournalReader
    snapshots: SnapshotReader
    max_records: int = DEFAULT_OBSERVATIONS_PER_TICK
    _rejected: int = field(default=0, init=False)
    _diagnostics: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_RETAINED_EVENTS))

    @property
    def rejected(self) -> int:
        """Records that could not be parsed as an observation. Counted, never kept."""
        return self._rejected

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    def drain(self, store: ObservationStore) -> int:
        """Push everything new into *store*. Returns how many it accepted."""
        accepted = 0
        if store.needs_full_snapshot:
            accepted += 1 if self._offer_snapshot(store) else 0
        read = self.reader.read()
        for diagnostic in read.diagnostics:
            self._diagnostics.append(diagnostic.detail)
        for record in read.records[: self.max_records]:
            try:
                observation = Observation.from_dict(record.payload)
            except ProtocolError as exc:
                self._rejected += 1
                self._diagnostics.append(f"unusable observation at offset {record.offset}: {exc}")
                continue
            if store.push(observation).accepted:
                accepted += 1
        return accepted

    def _offer_snapshot(self, store: ObservationStore) -> bool:
        read = self.snapshots.read()
        if isinstance(read, SnapshotMiss):
            for diagnostic in read.diagnostics:
                self._diagnostics.append(diagnostic)
            return False
        try:
            observation = Observation.from_dict(read.document)
        except ProtocolError as exc:
            self._rejected += 1
            self._diagnostics.append(f"unusable snapshot in slot {read.slot.value}: {exc}")
            return False
        return store.push(observation).accepted


@dataclass
class QueueCommandSink:
    """The engine's :class:`~pz_agent_core.actions.engine.CommandSink`, over the journal.

    It is the *only* consumer of the ack stream in the process. The engine drains
    acks while it drives one command and the loop drains them between commands;
    if both held their own reader, each would consume records the other needed
    and the queue's sequencing would report gaps that never happened.
    """

    queue: CommandQueue
    clock: Clock = system_clock_ms
    _progress_ms: dict[str, int] = field(default_factory=dict, init=False)
    _cancelled: int = field(default=0, init=False)

    @property
    def cancellations(self) -> int:
        return self._cancelled

    def last_progress_ms(self, command: Command) -> int:
        """When this command was last heard from; its issue time until then."""
        return self._progress_ms.get(command.command_id, command.issued_at_ms)

    def send(self, command: Command) -> Dispatch:
        outcome = self.queue.submit(command)
        if outcome.accepted:
            self._progress_ms[outcome.command.command_id] = self.clock()
            return Dispatch(command=outcome.command)
        return Dispatch(command=outcome.command, rejection=outcome.terminal_result)

    def poll_acks(self) -> Sequence[ActionResult]:
        poll = self.queue.poll_acks()
        now = self.clock()
        for result in poll.results:
            self._progress_ms[result.command_id] = now
            if result.is_terminal:
                self._progress_ms.pop(result.command_id, None)
        return poll.results

    def cancel(self, command: Command, reason: ReasonCode) -> None:
        """Ask the mod to abandon *command*.

        ``plan.cancel`` rather than ``safety.stop``: this is one command being
        withdrawn, and a stop would clear everything the mod is holding on the
        agent's behalf — including work the loop is still driving.
        """
        withdrawal = self.queue.build(
            ActionName.PLAN_CANCEL,
            idempotency_key=f"cancel-{command.command_id}-{reason.value}"[:120],
            lease_ms=5_000,
            args={"command_id": command.command_id},
        )
        self.queue.submit(withdrawal)
        self._cancelled += 1


@dataclass
class JournalObservationSource:
    """The engine's :class:`~pz_agent_core.actions.engine.ObservationSource`.

    ``wait_for_next`` is a bounded poll rather than a blocking read: the exchange
    is a directory of files, so there is nothing to block on, and the bound on
    the iteration count exists because the clock is injected — a frozen one would
    otherwise turn the deadline into a spin.
    """

    feed: ObservationFeed
    store: ObservationStore
    clock: Clock = system_clock_ms
    sleep: Sleeper = system_sleep_ms
    poll_interval_ms: int = 25
    max_polls: int = MAX_SOURCE_POLLS

    def __post_init__(self) -> None:
        if self.poll_interval_ms < 0:
            raise LoopError(f"poll_interval_ms must be non-negative, got {self.poll_interval_ms}")
        if self.max_polls < 1:
            raise LoopError(f"max_polls must be positive, got {self.max_polls}")

    def latest(self) -> Observation | None:
        return self.store.latest()

    def wait_for_next(self, after_seq: int, timeout_ms: int) -> Observation | None:
        deadline = self.clock() + max(0, timeout_ms)
        for _ in range(self.max_polls):
            self.feed.drain(self.store)
            latest = self.store.latest()
            if latest is not None and (after_seq == ANY_SEQ or latest.seq > after_seq):
                return latest
            if self.clock() >= deadline:
                return None
            self.sleep(self.poll_interval_ms)
        return None


# ---------------------------------------------------------------------------
# the capability ledger
# ---------------------------------------------------------------------------

#: Where the sidecar writes what it resolved about the game's API, under the
#: state directory beside the pid and control files. Deliberately not in the
#: exchange directory: uninstalling the mod must not erase the record of what
#: the last session could and could not do.
CAPABILITY_FILE_NAME: Final = "sidecar.capabilities.json"


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """What one sidecar established about the game's API, as ``status`` reads it.

    ``resolved`` is the field nothing else may be inferred from. A session with
    no usable capabilities because the scan found none and a session with no
    usable capabilities because the scan never ran produce the same empty list
    and opposite facts, and only the second one is something to go and fix. A
    sidecar that quietly allowed everything after a failed scan would be the
    assumption :func:`~pz_agent_core.actions.engine.deny_capability` exists to
    prevent; a sidecar that quietly refused everything without saying so would
    be the same silence pointed the other way.
    """

    resolved: bool
    detail: str
    build: str = ""
    revision: int = 0
    usable: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    refused: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @classmethod
    def unresolved(cls, detail: str, *, notes: Sequence[str] = ()) -> CapabilityRecord:
        """The record for a session that established nothing. *detail* says why."""
        if not detail:
            raise LoopError("an unresolved capability record must say why it is unresolved")
        return cls(resolved=False, detail=detail, notes=tuple(notes)[:MAX_NOTES])

    @classmethod
    def from_report(
        cls,
        report: CapabilityReport,
        detail: str,
        *,
        notes: Sequence[str] = (),
        disabled: Collection[str] = (),
    ) -> CapabilityRecord:
        """Summarise *report* for the status readout.

        ``verified`` is listed separately from ``usable`` even though every
        verified capability is usable: they are different claims. Usable means
        the symbols are on this install; verified means an action actually ran
        and its postcondition was observed in this session.

        A capability the user switched off is removed from ``usable`` and given
        a reason in ``refused``, so ``status`` says *why* rather than leaving a
        name that silently stopped appearing. It stays in ``verified`` when it
        is: a live ack happened, and the record of that is evidence about the
        install rather than about the current setting.
        """
        verified = tuple(
            sorted(c.name for c in report.capabilities if c.state is CapabilityState.VERIFIED)
        )
        switched_off = frozenset(disabled)
        refused = dict(report.unusable_reasons())
        for name in sorted(switched_off):
            refused[name] = "switched off in config.toml (safety.disabled_capabilities)"
        return cls(
            resolved=True,
            detail=detail,
            build=report.build,
            revision=report.revision,
            usable=tuple(n for n in report.usable_names() if n not in switched_off),
            verified=verified,
            refused=refused,
            notes=tuple(notes)[:MAX_NOTES],
        )

    def to_dict(self) -> JsonDict:
        return {
            "resolved": self.resolved,
            "detail": self.detail,
            "build": self.build,
            "revision": self.revision,
            "usable": list(self.usable),
            "verified": list(self.verified),
            "refused": dict(self.refused),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CapabilityRecord:
        """Parse a record this process did not necessarily write.

        Raises:
            LoopError: for anything malformed. A half-understood record would be
                read as "resolved" by whichever field happened to survive, and
                that is the direction that lies.
        """
        resolved = payload.get("resolved")
        detail = payload.get("detail")
        if not isinstance(resolved, bool):
            raise LoopError("a capability record must say whether capabilities were resolved")
        if not isinstance(detail, str):
            raise LoopError("a capability record must carry a detail string")
        revision = payload.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise LoopError("a capability record's revision must be an integer")
        refused = payload.get("refused", {})
        if not isinstance(refused, Mapping):
            raise LoopError("a capability record's refusals must be an object")
        return cls(
            resolved=resolved,
            detail=detail,
            build=_as_text(payload.get("build", ""), "build"),
            revision=revision,
            usable=_as_names(payload.get("usable", []), "usable"),
            verified=_as_names(payload.get("verified", []), "verified"),
            refused={
                _as_text(name, "refused key"): _as_text(reason, "refused reason")
                for name, reason in refused.items()
            },
            notes=_as_names(payload.get("notes", []), "notes")[:MAX_NOTES],
        )


def _as_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise LoopError(f"a capability record's {field_name} must be a string")
    return value


def _as_names(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise LoopError(f"a capability record's {field_name} must be an array")
    return tuple(_as_text(item, field_name) for item in value)


def read_capability_record(path: Path) -> CapabilityRecord | None:
    """The record at *path*, or None when there is nothing usable to read.

    None means "no sidecar has resolved capabilities in this state directory",
    which is a different statement from "capabilities were not resolved" and is
    reported as such: nothing has run yet, so nothing has been established.
    """
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        return CapabilityRecord.from_dict(payload)
    except LoopError:
        return None


def _mod_shaped_ack(result: ActionResult) -> ActionResult | None:
    """The engine's own result, re-stated at the level a probe reads.

    :meth:`~pz_agent_core.actions.engine.ActionEngine._success` attaches
    ``Evidence.to_payload()``, so a sidecar ``ActionResult`` carries the observed
    values one level down under ``observed``, beside the adapter's ``kind`` and
    the observation sequence. :meth:`RuntimeConfirmation.missing_keys` is a
    top-level exact match, so handing it that document reports *every* declared
    key missing, for every probe, and silently — the capability would simply
    never verify. ``tests/contract/test_capability_evidence_agreement.py`` pins
    both shapes; this returns the one that fits.
    """
    observed = result.evidence.get("observed")
    if not isinstance(observed, dict) or not observed:
        return None
    return replace(result, evidence=dict(observed))


@dataclass
class CapabilityLedger:
    """The single answer to "may this capability be used", and the files it keeps.

    Two documents, because they answer different questions. The *report* at
    :attr:`report_path` is the evidence ledger ``doctor`` generates and this
    class updates as probes confirm — it survives restarts and carries the scan
    findings each claim rests on. The *record* at :attr:`record_path` is this
    session's summary of it, written where ``status`` can read it without
    re-scanning a Lua tree.

    ``report is None`` is the fail-closed state: the scan could not run, so
    :meth:`usable` answers False to everything and :attr:`detail` says why. That
    is the whole reason this class exists rather than a bare ``report.usable``
    bound method — a scan that fails has no report to bind to, and the obvious
    fallback of allowing everything is the assumption the capability system was
    built to refuse.
    """

    record_path: Path
    report: CapabilityReport | None = None
    detail: str = ""
    report_path: Path | None = None
    #: Directories this project only ever reads. The game install goes here, so
    #: a confirmation can never write the report inside it.
    protected_roots: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()
    #: Capabilities the user switched off in ``config.toml``. The one input to
    #: this class that is not evidence — it is a decision, and it only ever
    #: subtracts. ``CapabilityState.DISABLED_BY_POLICY`` and the refusal
    #: ``PermissionEngine`` prints for it ("switched off by configuration")
    #: existed, and both were unreachable: no configuration could produce the
    #: state, while ``docs/COMPATIBILITY.md`` — which ships in the Windows
    #: archive — listed it as "available, but configuration forbids it" three
    #: rows above its own warning that ``survival_sleep`` cannot be reached by a
    #: panic stop.
    disabled: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.detail:
            raise LoopError("a capability ledger must say how it reached its report, or why not")

    @property
    def resolved(self) -> bool:
        return self.report is not None

    def usable(self, name: str) -> bool:
        """The :data:`~pz_agent_core.actions.engine.CapabilityCheck` for this session.

        The disabled set is applied *here* rather than by editing the report,
        because the report is the evidence ledger: it records what the install
        can do, and a user's decision is not a finding about the install. It
        also means a capability switched off and then switched back on needs no
        rescan to become usable again.
        """
        if name in self.disabled:
            return False
        report = self.report
        return report is not None and report.usable(name)

    def record(self) -> CapabilityRecord:
        report = self.report
        if report is None:
            return CapabilityRecord.unresolved(self.detail, notes=self.notes)
        return CapabilityRecord.from_report(
            report, self.detail, notes=self.notes, disabled=self.disabled
        )

    def publish(self) -> CapabilityRecord:
        """Write the record where ``status`` reads it. Returns what was written.

        A record that cannot be written is noted and not raised: the sidecar
        losing its status file is not a reason to stop driving the game, and the
        note travels with the next attempt.
        """
        record = self.record()
        try:
            _write_json(self.record_path, record.to_dict())
        except OSError as exc:
            self._note(f"the capability record could not be written ({exc.strerror or exc})")
        return record

    def confirm_capability(
        self, capability: str, action: ActionName, result: ActionResult
    ) -> Capability | None:
        """Fold one action's outcome into *capability*'s claim.

        Returns the capability as it now stands, or None when this result says
        nothing about it. The distinction matters more than it looks: several
        adapters share one capability, and only the action a probe names as its
        own confirmation carries proof about it. ``movement.move_near`` and
        ``container.open`` both require ``move_to_square``, whose probe confirms
        on ``movement.move_to`` — handing their acks to
        :func:`~pz_agent_core.capabilities.probes.confirm` would be handing it an
        ack for the wrong action, which it rightly refuses, and the refusal
        *demotes*. A successful sidestep would then destroy a verification a
        real walk had earned a tick earlier.
        """
        report = self.report
        if report is None or result.status is not ActionStatus.SUCCEEDED:
            return None
        try:
            probe = probe_for(capability)
        except CapabilityError as exc:
            # An adapter naming a capability no probe declares. Nothing can ever
            # confirm it, and the mismatch is a defect worth seeing in status.
            self._note(str(exc))
            return None
        if probe.confirmation.action is not action:
            return None
        static = report.get(capability)
        if static is None:
            self._note(f"{capability}: the report holds no entry for this probe to confirm")
            return None
        ack = _mod_shaped_ack(result)
        if ack is None:
            self._note(f"{capability}: the result carried no observed values to confirm it with")
            return None
        confirmed = confirm(probe, static, ack, build=report.build)
        self.report = report.with_capabilities([confirmed])
        self._persist_report()
        self.publish()
        return confirmed

    def _persist_report(self) -> None:
        path = self.report_path
        report = self.report
        if path is None or report is None:
            return
        try:
            save_report(path, report, protected_roots=self.protected_roots)
        except (ReportIOError, ScanError) as exc:
            # ``ScanError`` covers ``WriteRefused``: the report path resolving
            # inside the game directory is a wiring defect, and it is reported
            # rather than allowed to end the session.
            self._note(f"the capability report could not be written ({exc})")

    def _note(self, note: str) -> None:
        self.notes = (*self.notes, note)[-MAX_NOTES:]


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachOutcome:
    """The result of attaching to an exchange directory."""

    attached: bool
    detail: str
    session: SessionDescriptor | None = None
    resumed: bool = False


class StopCause(StrEnum):
    """Why :meth:`SidecarLoop.run` returned. Never "it just did"."""

    TICK_BUDGET = "tick_budget"
    STOP_REQUESTED = "stop_requested"
    SESSION_ENDED = "session_ended"
    CALLER_ASKED = "caller_asked"


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """Everything one tick observed and did."""

    tick: int
    now_ms: int
    mode: SessionMode
    armed: bool
    game_alive: bool
    panic: bool
    ingested: int
    events: tuple[SafetyEvent, ...] = ()
    lost: tuple[ActionResult, ...] = ()
    results: tuple[ActionResult, ...] = ()
    disarmed: bool = False
    stop: StopCause | None = None
    detail: str = ""
    #: State-directory writes that failed during this tick (E12-M03-T004). The
    #: session outranks its own bookkeeping, so these are reported here rather
    #: than raised — and they must be *somewhere*, because a pid record that
    #: quietly stops refreshing reads as a dead sidecar to everything else.
    state_problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What a whole :meth:`SidecarLoop.run` did."""

    ticks: int
    cause: StopCause
    detail: str
    last: TickOutcome | None = None


@dataclass(frozen=True, slots=True)
class ArmOutcome:
    """Whether an arm or disarm request took effect, and why not if it did not.

    ``pending`` marks the one answer that is neither: the arm passed every
    local gate and a ``session.arm`` command is on its way to the mod, but no
    authority has been granted — ``armed`` stays False until the game's own
    terminal ack and a heartbeat reporting the armed mode are both observed
    (see :meth:`SidecarLoop.arm`). A caller that reads ``armed`` alone still
    reads the truth: nothing is armed yet.
    """

    armed: bool
    mode: SessionMode
    detail: str
    changed: bool = False
    pending: bool = False


@dataclass(frozen=True, slots=True)
class PendingArm:
    """One arm awaiting the game's confirmation; see :meth:`SidecarLoop.arm`.

    Immutable and replaced whole (the one transition, ``acked``, goes through
    :func:`dataclasses.replace`), so the Core RPC serving thread may read
    :attr:`SidecarLoop.pending_arm` without a lock. ``nonce`` ties the arm back
    to the control request that asked for it, when one did — that is how the
    resolution reaches :class:`ControlDecision` only once the outcome is known.
    """

    command_id: str
    idempotency_key: str
    mode: SessionMode
    session_id: str
    requested_at_ms: int
    deadline_ms: int
    nonce: str | None = None
    #: The mod's terminal ``succeeded`` ack has been seen; the confirming
    #: heartbeat has not. Kept so the deadline refusal can name which half of
    #: the confirmation went missing.
    acked: bool = False


@dataclass(frozen=True, slots=True)
class DisarmNotice:
    """What became of one ``session.disarm`` sent to the game.

    Disarming is never gated — the sidecar drops its own authority before this
    command is even built — so this record carries no authority either way. It
    exists because the opposite silence was the live finding's mirror image: a
    sidecar that reports ``disarmed`` while the game still holds ``ASSISTED``
    is lying by omission, and the only honest states are "the mod confirmed it
    dropped to OFF", "the mod refused and said why", and "nothing confirmed
    anything within the bound".
    """

    command_id: str
    confirmed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PausedGoal:
    """The active goal as it stood when the user took over.

    §8: user input always wins, and the goal channel's only lever for that
    today is a cancel — the goals package has no ``PAUSED`` state. This marker
    is the loop's honest bridge: the goal's record in the queue ends as
    ``CANCELLED`` (through the queue's own vocabulary, nothing invented), and
    the loop keeps *this* beside it so status can say "paused by your
    takeover, not abandoned by the agent". Nothing resumes it implicitly — a
    new arm does not, on purpose — resuming is an explicit fresh submission.
    """

    goal_id: str
    kind: str
    reason: str
    paused_at_ms: int


@dataclass(frozen=True, slots=True)
class ShutdownOutcome:
    """What shutting down released."""

    lock_released: bool
    detail: str
    lost: tuple[ActionResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """What the loop did with one control request, published for another thread.

    The control channel is one-way: a second terminal (or the Core RPC server's
    thread) writes a request file and the loop consumes it on its next tick,
    which leaves the writer with no way to learn what the loop decided — an arm
    can be refused for a stale request, a silent game, a panic sentinel or a
    multiplayer session, and until this record existed every one of those read
    back as nothing at all. The loop publishes its judgement here, keyed by the
    request's nonce, so the writer can wait a bounded time and then report the
    loop's *own* words rather than re-deriving a second opinion that races the
    first.

    One slot, not a log, mirroring the channel itself: the channel holds at
    most one pending request, so at most one decision is in flight, and a
    request that was replaced before the loop saw it never gets a decision —
    its writer times out and says so. The instance is immutable and the field
    holding it is replaced whole, which is what makes the cross-thread read
    safe: a reader sees the previous decision or this one, never half of each.
    """

    nonce: str
    kind: ControlKind
    armed: bool
    mode: SessionMode
    detail: str
    applied: bool


# ---------------------------------------------------------------------------
# the remote action channel
# ---------------------------------------------------------------------------

#: Remote submissions that may wait for the loop at once. Small on purpose, for
#: the same reason the goal backlog is: an action is an imperative about the
#: world *now*, and a deep backlog of them is a queue of stale intents by the
#: time the loop gets to the fourth.
DEFAULT_MAX_PENDING_ACTIONS: Final = 4

#: Records kept so ``action.status`` can answer for an action that has already
#: ended. Long enough that a client polling at a sane rate never loses the id
#: it is waiting on; short enough to be a ring, not a log.
DEFAULT_MAX_REMEMBERED_ACTIONS: Final = 32

#: The lease a submission is held to when its request somehow carries none.
#: :class:`~pz_agent_core.actions.engine.ActionRequest` bounds its own
#: ``lease_ms`` at construction, so this default is a belt for the seam, not a
#: path anything ordinary takes — but a record admitted without a bound would
#: be the one thing in the channel allowed to live forever.
DEFAULT_ACTION_LEASE_MS: Final = 30_000

#: Grace past a submission's lease before the sweep ends it. The lease is the
#: caller's own timeout (``router.py`` passes the tool's ``timeout_ms``), and
#: a dispatch that started just inside it deserves the engine's answer rather
#: than a racing sweep; one game-heartbeat timeout of slack is enough for
#: that and never enough to matter to a caller that has already given up.
ACTION_SWEEP_GRACE_MS: Final = 5_000


def mint_action_id() -> str:
    """A fresh remote action id.

    Minted here rather than accepted from the submitter for the same reason
    :func:`~pz_agent_core.goals.model.mint_goal_id` is: the id comes back in
    refusals and status answers, and an identifier a caller chose is an
    identifier a caller could fill with text.
    """
    return str(uuid.uuid4())


class ActionChannel:
    """The bounded cross-thread submission path for remote actions.

    The module docstring names the two halves this channel exists to provide —
    a bounded queue the loop drains plus a record store for in-flight ids —
    because anything less is a port that accepts work and invents its progress.
    The shape mirrors :class:`~pz_agent_core.goals.GoalQueue`, the channel that
    already crossed this seam, with one deliberate difference: the lock lives
    *inside* this class rather than on the loop, because unlike the queue this
    class was written for two threads from the start. Every touch of its state
    happens under :attr:`_lock`, every hold is a handful of in-memory
    dictionary operations — no IO, no engine call, no sleep — and the records
    handed out are frozen :class:`~pz_agent_mcp.ports.ActionRecord` instances
    replaced whole on every transition, so a reader holds a consistent record
    or the next one, never half of each.

    What each side does:

    * The Core RPC serving thread calls :meth:`submit` and :meth:`status`.
      Admission is non-blocking and honest three ways: a full queue is a raised
      refusal naming the cap, never a silent drop; a resubmitted idempotency
      key resolves to the record it already created, so a retried submission —
      the ordinary consequence of a dropped answer — never runs twice; a key
      reused for *different* content is refused, because that is a caller bug
      and silently serving the first request would hide it.
    * The tick thread calls :meth:`take_next` before it drives the engine and
      :meth:`settle` with the engine's own terminal result afterwards, and
      :meth:`clear_pending` when a stop lever ends what is still waiting. The
      engine is never called under the lock — :meth:`take_next` marks the
      record ``STARTED`` and releases before the loop touches the engine.

    Bounds: at most :attr:`max_pending` submissions wait, at most
    :attr:`max_remembered` records are held, and eviction only ever removes a
    terminal record — forgetting an action that is still waiting or running
    would lose the only handle its submitter has on it, so the constructor
    requires ``max_remembered > max_pending`` to guarantee a terminal victim
    exists whenever the cap is exceeded.
    """

    def __init__(
        self,
        *,
        clock: Clock = system_clock_ms,
        max_pending: int = DEFAULT_MAX_PENDING_ACTIONS,
        max_remembered: int = DEFAULT_MAX_REMEMBERED_ACTIONS,
        new_id: Callable[[], str] = mint_action_id,
        default_lease_ms: int = DEFAULT_ACTION_LEASE_MS,
        sweep_grace_ms: int = ACTION_SWEEP_GRACE_MS,
    ) -> None:
        if max_pending < 1:
            raise LoopError(f"max_pending must be positive, got {max_pending}")
        if max_remembered <= max_pending:
            # `<=` rather than `<`: beside a full queue one further record can
            # be non-terminal (the submission the tick thread has taken and is
            # driving), and eviction must still find a terminal victim then.
            raise LoopError(
                f"max_remembered must exceed max_pending ({max_pending}), got {max_remembered}"
            )
        if default_lease_ms < 1:
            raise LoopError(f"default_lease_ms must be positive, got {default_lease_ms}")
        if sweep_grace_ms < 0:
            raise LoopError(f"sweep_grace_ms must be non-negative, got {sweep_grace_ms}")
        self._clock = clock
        self._new_id = new_id
        self._max_pending = max_pending
        self._max_remembered = max_remembered
        self._default_lease_ms = default_lease_ms
        self._sweep_grace_ms = sweep_grace_ms
        self._lock = threading.Lock()
        #: Every record this channel has minted and not yet evicted, oldest
        #: first. Values are frozen and replaced whole; see the class docstring.
        self._records: OrderedDict[str, ActionRecord] = OrderedDict()
        #: The requests behind those records, kept in step with them (evicted
        #: together) so a resubmitted key can be compared against what its
        #: first use actually asked for.
        self._requests: dict[str, ActionRequest] = {}
        #: Submissions the tick thread has not taken yet, oldest first.
        self._pending: OrderedDict[str, ActionRequest] = OrderedDict()
        #: action id -> (lease applied, wall deadline). Held only while the
        #: record is non-terminal: the deadline is what the sweep enforces, and
        #: a record that already carries its terminal answer has nothing left
        #: to bound. Trimmed with the record store.
        self._deadlines: dict[str, tuple[int, int]] = {}
        #: idempotency key -> action id, trimmed with the record store.
        self._by_key: dict[str, str] = {}
        #: Sequence numbers for the refusal results this channel synthesises
        #: itself, kept apart from the mod's ack stream for the same reason the
        #: engine keeps its own: a locally minted result never appeared there.
        self._seq = 0

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def has_pending(self) -> bool:
        """Whether a submission is waiting. A single read the tick thread polls."""
        with self._lock:
            return bool(self._pending)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def remembered_count(self) -> int:
        with self._lock:
            return len(self._records)

    def submit(self, request: ActionRequest) -> ActionRecord:
        """Admit *request* to the queue, or refuse by name. Never waits.

        Returns the record as admitted — ``accepted``, which is the honest word
        for queued — or the record a resubmitted key already created.

        Raises:
            LoopError: the queue is full (the refusal names the cap), or the
                idempotency key was already used for a different request.
        """
        with self._lock:
            existing_id = self._by_key.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is None:
                    # The record was evicted while its key survived; there is
                    # nothing left to hand back, so the key starts over.
                    self._by_key.pop(request.idempotency_key, None)
                elif self._requests.get(existing_id) == request:
                    return existing
                else:
                    raise LoopError(
                        "that idempotency key was already used for a different action "
                        "request; mint a new key for new work"
                    )
            if len(self._pending) >= self._max_pending:
                raise LoopError(
                    f"the remote action queue already holds {self._max_pending} "
                    "submission(s), which is its cap; wait for one to finish or check "
                    "'action.status' before submitting more"
                )
            record = ActionRecord(
                action_id=self._new_id(),
                action=request.action,
                status=ActionStatus.ACCEPTED,
                idempotency_key=request.idempotency_key,
            )
            # Every record is admitted with a wall deadline: the request's own
            # lease (the caller's tool timeout, via router.py) when it carries
            # one, the channel's bounded default when it somehow does not. An
            # ``accepted`` that nothing ever bounds is an id its submitter
            # polls forever; see :meth:`sweep`.
            lease = request.lease_ms if request.lease_ms > 0 else self._default_lease_ms
            self._deadlines[record.action_id] = (
                lease,
                self._clock() + lease + self._sweep_grace_ms,
            )
            self._records[record.action_id] = record
            self._requests[record.action_id] = request
            self._pending[record.action_id] = request
            self._by_key[request.idempotency_key] = record.action_id
            self._evict()
            return record

    def status(self, action_id: str) -> ActionRecord | None:
        """The record for *action_id*, or None for an id unknown to this channel.

        None covers exactly three truths, all of them "this process cannot
        say more": the id was never minted here, it was evicted after its
        record turned terminal, or it was minted by a previous process (a
        restarted sidecar starts with an empty store). It never means "still
        running" — a live record always answers — so a boundary serving this
        port can honestly tell "unknown id" apart from any lifecycle state.
        """
        with self._lock:
            return self._records.get(action_id)

    def take_next(self) -> tuple[str, ActionRequest] | None:
        """The oldest waiting submission, marked ``started``, for the tick thread.

        The caller drives the engine *after* this returns — never under the
        lock — and must follow up with :meth:`settle`, which is what turns the
        ``started`` claim into the engine's own terminal answer.
        """
        with self._lock:
            if not self._pending:
                return None
            action_id, request = self._pending.popitem(last=False)
            self._records[action_id] = replace(
                self._records[action_id], status=ActionStatus.STARTED
            )
            return action_id, request

    def settle(self, action_id: str, result: ActionResult) -> ActionRecord:
        """Record the engine's terminal *result* against *action_id*.

        The status recorded is the result's own — the engine's success carries
        the observed postcondition :class:`~pz_agent_mcp.ports.ActionRecord`
        demands, and every refusal carries the engine's reason — so nothing is
        decided here, only remembered.

        Raises:
            LoopError: *result* is not terminal (a record that says
                ``succeeded``-later would be the early claim this project
                forbids), or *action_id* was never minted, which is a loop bug
                rather than a runtime condition.
        """
        if not result.is_terminal:
            raise LoopError(f"only a terminal result settles an action; got {result.status.value}")
        with self._lock:
            record = self._records.get(action_id)
            if record is None:
                raise LoopError("settle was called for an action id this channel never minted")
            settled = replace(record, status=result.status, result=result, progress=result.progress)
            self._records[action_id] = settled
            self._records.move_to_end(action_id)
            self._deadlines.pop(action_id, None)
            self._evict()
            return settled

    def clear_pending(
        self, reason_code: ReasonCode, *, status: ActionStatus, message: str
    ) -> tuple[ActionRecord, ...]:
        """End every waiting submission with a terminal record that says why.

        The stop levers' half of the channel: a disarm, a panic sentinel and a
        shutdown all land here, each with its own reason, mirroring
        :meth:`~pz_agent_core.goals.GoalQueue.disarm` and
        :meth:`~pz_agent_core.goals.GoalQueue.panic_stop`. Only *waiting*
        submissions are touched — one the tick thread has already taken is the
        engine's to finish, and the engine applies the same levers itself.
        """
        if status is ActionStatus.SUCCEEDED or not status.is_terminal:
            raise LoopError(f"clearing must end submissions, not mark them {status.value}")
        with self._lock:
            ended: list[ActionRecord] = []
            while self._pending:
                action_id, request = self._pending.popitem(last=False)
                self._seq += 1
                refusal = ActionResult.failure(
                    session_id=request.session_id,
                    seq=self._seq,
                    command_id=action_id,
                    action=request.action.value,
                    timestamp_ms=self._clock(),
                    reason_code=reason_code,
                    status=status,
                    message=message,
                )
                settled = replace(self._records[action_id], status=status, result=refusal)
                self._records[action_id] = settled
                self._records.move_to_end(action_id)
                self._deadlines.pop(action_id, None)
                ended.append(settled)
            self._evict()
            return tuple(ended)

    def sweep(self, now_ms: int) -> tuple[ActionRecord, ...]:
        """End every record that outlived its lease plus grace. Bounded, honest.

        The tick thread calls this once per tick. An ``accepted`` record can
        wait indefinitely on gates that never open — a silent game, a spent
        budget, a world with no observation — and a ``started`` one can be
        orphaned by a crash between :meth:`take_next` and :meth:`settle`. Both
        end here as ``FAILED`` / ``ACTION_TIMEOUT`` with a message that says
        which state they timed out in, because a submitter polling the id must
        eventually read a terminal answer, not a promise that outlived its own
        deadline. Nothing that is inside its lease is touched.
        """
        with self._lock:
            ended: list[ActionRecord] = []
            for action_id, record in list(self._records.items()):
                if record.terminal:
                    continue
                bound = self._deadlines.get(action_id)
                if bound is None:
                    continue
                lease, deadline = bound
                if now_ms < deadline:
                    continue
                request = self._requests[action_id]
                waiting = self._pending.pop(action_id, None) is not None
                phase = (
                    "while waiting for a tick to serve it"
                    if waiting
                    else "after it was started, without a terminal result"
                )
                self._seq += 1
                timeout = ActionResult.failure(
                    session_id=request.session_id,
                    seq=self._seq,
                    command_id=action_id,
                    action=request.action.value,
                    timestamp_ms=now_ms,
                    reason_code=ReasonCode.ACTION_TIMEOUT,
                    status=ActionStatus.FAILED,
                    message=(
                        f"no terminal outcome was observed within the {lease} ms lease "
                        f"plus {self._sweep_grace_ms} ms grace; the submission timed "
                        f"out {phase}"
                    ),
                )
                settled = replace(record, status=ActionStatus.FAILED, result=timeout)
                self._records[action_id] = settled
                self._records.move_to_end(action_id)
                self._deadlines.pop(action_id, None)
                ended.append(settled)
            if ended:
                self._evict()
            return tuple(ended)

    def close_open(self, reason_code: ReasonCode, *, message: str) -> tuple[ActionRecord, ...]:
        """End *every* non-terminal record, waiting or started, and say why.

        The re-attach lever. :meth:`clear_pending` deliberately leaves a taken
        submission to the engine that is driving it — but across an attach
        there is no such engine any more: the records belong to a session that
        ended, and a client polling one of their ids must read a terminal
        answer rather than an ``accepted`` frozen forever. A waiting record
        ends ``CANCELLED`` (nothing ever ran); a started one ends ``LOST``,
        because whether the previous attachment's work finished is a fact
        nobody observed.
        """
        with self._lock:
            ended: list[ActionRecord] = []
            for action_id, record in list(self._records.items()):
                if record.terminal:
                    continue
                request = self._requests[action_id]
                waiting = self._pending.pop(action_id, None) is not None
                status = ActionStatus.CANCELLED if waiting else ActionStatus.LOST
                self._seq += 1
                refusal = ActionResult.failure(
                    session_id=request.session_id,
                    seq=self._seq,
                    command_id=action_id,
                    action=request.action.value,
                    timestamp_ms=self._clock(),
                    reason_code=reason_code,
                    status=status,
                    message=message,
                )
                settled = replace(record, status=status, result=refusal)
                self._records[action_id] = settled
                self._records.move_to_end(action_id)
                self._deadlines.pop(action_id, None)
                ended.append(settled)
            if ended:
                self._evict()
            return tuple(ended)

    def _evict(self) -> None:
        """Drop the oldest terminal records until the store fits its cap.

        Called under :attr:`_lock`. A waiting or running record is never the
        victim — forgetting one would lose the submitter's only handle on it —
        and the constructor's ``max_remembered > max_pending`` bound is what
        guarantees a terminal victim exists whenever the cap is exceeded.
        """
        while len(self._records) > self._max_remembered:
            victim = next(
                (aid for aid, record in self._records.items() if record.terminal),
                None,
            )
            if victim is None:
                return
            record = self._records.pop(victim)
            self._requests.pop(victim, None)
            self._deadlines.pop(victim, None)
            if self._by_key.get(record.idempotency_key) == victim:
                self._by_key.pop(record.idempotency_key, None)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


@dataclass
class _Attached:
    """The state that only exists once a session does."""

    session: SessionDescriptor
    queue: CommandQueue
    feed: ObservationFeed
    sink: QueueCommandSink
    source: JournalObservationSource
    engine: ActionEngine
    attached_at_ms: int


@dataclass
class SidecarLoop:
    """Attach, observe, guard, and — only when explicitly armed — act."""

    layout: IpcLayout
    state_dir: Path
    registry: AdapterRegistry
    clock: Clock = system_clock_ms
    sleep: Sleeper = system_sleep_ms
    limits: LoopLimits = DEFAULT_LIMITS
    guard: ReflexGuard = field(default_factory=ReflexGuard)
    planner: Planner | None = None
    capability_check: CapabilityCheck = deny_capability
    capabilities: CapabilityLedger | None = None
    sessions: SessionManager | None = None
    monitor: HeartbeatMonitor | None = None
    lock: SidecarLock | None = None
    pid_file: PidFile | None = None
    control: ControlChannel | None = None
    #: Where this run is recorded for ``pz-agent replay``. Optional, because the
    #: loop drives a character and a diagnostic is never a reason not to.
    trace: TraceWriter | None = None
    #: The typed goal channel, when this sidecar serves one. ``None`` is a loop
    #: with no channel — the Core RPC adapter then leaves ``goals`` unserved
    #: rather than admitting goals nothing would ever activate.
    goals: GoalQueue | None = None
    #: The remote action channel, filled in by ``__post_init__`` when the
    #: caller passes nothing: every loop owns an engine once attached, so every
    #: loop can honestly serve this — unlike the goal channel, whose serving
    #: needs a planner the assembly may not have. ``None`` remains expressible
    #: (assign it after construction) and means what it says: the Core RPC
    #: adapter then leaves ``actions`` as the named refusal rather than
    #: admitting submissions nothing would ever drain.
    actions: ActionChannel | None = None
    #: Where the goal channel survives this process, when it does. Set by
    #: :meth:`adopt_goal_store` — never assigned directly, because adoption is
    #: also the restore — and left ``None`` for a loop whose goals are
    #: deliberately mortal (most tests, and any loop without a queue).
    goal_store: GoalStore | None = None
    #: The one seam through which two threads reach :attr:`goals`.
    #: :class:`~pz_agent_core.goals.GoalQueue` is not thread-safe, so every
    #: touch of it — the tick thread's activation, settlement, tick, disarm and
    #: panic clear; the Core RPC serving thread's submit, status and cancel —
    #: happens under this lock and under nothing else. Every hold is short and
    #: bounded by construction: the queue's operations are pure in-memory
    #: transitions over at most ``max_remembered`` records, and no IO, no
    #: sleep, no planner call and no engine call ever happens while it is held.
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    _attached: _Attached | None = field(default=None, init=False)
    _mode: SessionMode = field(default=SessionMode.OBSERVE, init=False)
    _armed: bool = field(default=False, init=False)
    _pending_arm: PendingArm | None = field(default=None, init=False)
    _arm_resolution: ArmOutcome | None = field(default=None, init=False)
    _pending_disarm: PendingArm | None = field(default=None, init=False)
    _disarm_notice: DisarmNotice | None = field(default=None, init=False)
    _paused_goal: PausedGoal | None = field(default=None, init=False)
    #: The queue revision the goal store last wrote, so a tick that moved no
    #: goal writes no file. ``-1`` is "never written", distinct from the fresh
    #: queue's revision 0.
    _goal_flushed_revision: int = field(default=-1, init=False)
    _control_decision: ControlDecision | None = field(default=None, init=False)
    _tick: int = field(default=0, init=False)
    _budget: ActionBudget = field(init=False)
    _store: ObservationStore = field(init=False)
    _events: deque[SafetyEvent] = field(init=False)
    _shipped: OrderedDict[str, Command] = field(init=False)
    _traced: Observation | None = field(default=None, init=False)
    _state_problems: deque[str] = field(init=False)
    _fresh_problems: list[str] = field(init=False)

    def __post_init__(self) -> None:
        if self.capabilities is not None:
            if self.capability_check is not deny_capability:
                # Two answers to "may this run" is the defect this wiring exists
                # to close, so the loop refuses to hold both rather than picking
                # one and leaving the other looking connected.
                raise LoopError("pass either a capability ledger or a capability_check, not both")
            self.capability_check = self.capabilities.usable
        self._budget = ActionBudget(
            self.limits.max_actions_per_window, self.limits.action_window_ms
        )
        self._store = ObservationStore(capacity=self.limits.observation_window)
        self._events = deque(maxlen=MAX_RETAINED_EVENTS)
        self._shipped = OrderedDict()
        self._state_problems = deque(maxlen=MAX_RETAINED_EVENTS)
        self._fresh_problems = []
        if self.monitor is None:
            self.monitor = HeartbeatMonitor(self.layout, clock=self.clock)
        if self.sessions is None:
            self.sessions = SessionManager(self.layout, clock=self.clock, heartbeats=self.monitor)
        if self.control is None:
            self.control = ControlChannel(self.state_dir / "sidecar.control.json", clock=self.clock)
        if self.actions is None:
            self.actions = ActionChannel(clock=self.clock)

    # -- accessors ---------------------------------------------------------

    @property
    def mode(self) -> SessionMode:
        return self._mode

    @property
    def armed(self) -> bool:
        """Never True except after a *confirmed* :meth:`arm`. Not configurable."""
        return self._armed

    @property
    def pending_arm(self) -> PendingArm | None:
        """The arm awaiting the game's confirmation, if one is. Frozen, replaced whole."""
        return self._pending_arm

    @property
    def arm_resolution(self) -> ArmOutcome | None:
        """How the last two-phase arm resolved, for callers that did not travel
        the control channel. Written on the tick thread, replaced whole."""
        return self._arm_resolution

    @property
    def disarm_notice(self) -> DisarmNotice | None:
        """What the game said to the last ``session.disarm``; see :class:`DisarmNotice`."""
        return self._disarm_notice

    @property
    def paused_goal(self) -> PausedGoal | None:
        """The goal parked by the user's takeover, until a new goal is activated."""
        return self._paused_goal

    @property
    def store(self) -> ObservationStore:
        return self._store

    @property
    def ticks(self) -> int:
        return self._tick

    @property
    def recent_events(self) -> tuple[SafetyEvent, ...]:
        """The last safety events, oldest first. Bounded ring."""
        return tuple(self._events)

    @property
    def state_problems(self) -> tuple[str, ...]:
        """State-directory writes that failed mid-session, oldest first. Bounded ring.

        E12-M03-T004: when the state directory stops accepting writes, the
        session continues and the condition is reported — here, and on the
        :class:`TickOutcome` of every tick it happened in — rather than the
        loop dying on its own bookkeeping.
        """
        return tuple(self._state_problems)

    @property
    def session(self) -> SessionDescriptor | None:
        return None if self._attached is None else self._attached.session

    @property
    def control_decision(self) -> ControlDecision | None:
        """The loop's judgement of the last control request it consumed.

        Written on the tick thread, read from the Core RPC server's thread. The
        read is one reference to an immutable record, so it is safe without a
        lock; see :class:`ControlDecision`.
        """
        return self._control_decision

    @property
    def queue(self) -> CommandQueue | None:
        """The command stream for the current session, or None before attaching.

        Exposed because "what has the mod not answered yet" is a question the
        status readout and the diagnostics both ask, and the alternative is each
        of them opening a second reader over the ack journal — which would
        consume records this loop needs.
        """
        return None if self._attached is None else self._attached.queue

    def _heartbeats(self) -> HeartbeatMonitor:
        assert self.monitor is not None  # set in __post_init__
        return self.monitor

    def _session_manager(self) -> SessionManager:
        assert self.sessions is not None  # set in __post_init__
        return self.sessions

    def _control_channel(self) -> ControlChannel:
        assert self.control is not None  # set in __post_init__
        return self.control

    def _require_attached(self) -> _Attached:
        attached = self._attached
        if attached is None:
            raise LoopError("the loop has not attached to an exchange directory yet")
        return attached

    # -- attach and detach -------------------------------------------------

    def attach(self) -> AttachOutcome:
        """Take the lock and establish a session, in ``OBSERVE``.

        A session already on disk is resumed rather than replaced when the game
        confirms it, so a restarted sidecar keeps the generation its references
        were minted under. What it does not keep is authority: ``resume`` mints a
        new sidecar nonce and this method sets the mode to ``OBSERVE``
        unconditionally, on both paths.

        Attaching twice is refused, not layered. A second attach on a loop that
        is already attached would build a second :class:`_Attached` — a second
        ``JournalWriter`` over the same command journal, its sequences starting
        at zero — without closing the first: the in-process twin of the
        duplicate producer the cross-process :class:`SidecarLock` exists to
        refuse, and the callers that legitimately re-attach (``restart`` in the
        test worlds, a supervisor bringing a loop back) all shut down first.
        The refusal names the session so the caller knows what to shut down.
        """
        already = self._attached
        if already is not None:
            return AttachOutcome(
                attached=False,
                detail=(
                    f"this loop is already attached to session "
                    f"{already.session.session_id}; a second attach would open a second "
                    "producer over the same journals — shut down first"
                ),
            )
        self.layout.ensure()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        manager = self._session_manager()
        lock = self.lock
        if lock is None:
            # The lock has to be taken before the session file is touched, so it
            # records the session already on disk when there is one and says
            # "pending" when there is not. A random id would read like a session
            # that exists somewhere; "pending" is what is actually true.
            existing = manager.load()
            lock = SidecarLock(
                self.layout,
                session_id="pending" if existing is None else existing.session_id,
                clock=self.clock,
            )
            self.lock = lock
        claimed = lock.acquire()
        if not claimed.acquired:
            return AttachOutcome(
                attached=False,
                detail=f"another sidecar holds the lock: {claimed.detail}",
            )

        resume = manager.resume()
        resumed = resume.resumed
        if resumed and resume.session is not None:
            session = resume.session
        else:
            session = manager.create(mode=SessionMode.OBSERVE)
        self._mode = SessionMode.OBSERVE
        self._armed = False
        self._pending_arm = None
        self._pending_disarm = None
        if self.actions is not None:
            # Records left from before this attachment — a started submission a
            # crashed drive never settled, anything shutdown's clear could not
            # see — turn terminal now, so a client polling an old action id
            # after a re-attach reads an answer rather than ``accepted``
            # forever. The wording mirrors shutdown's clear_pending: what ended
            # this work is named, nothing about its outcome is invented.
            self.actions.close_open(
                ReasonCode.STALE_SESSION,
                message=(
                    f"the sidecar re-attached (session {session.session_id}) while "
                    "this action was open; its outcome was never observed"
                ),
            )
        self._attached = self._build(session)
        self._publish_heartbeat()
        detail = (
            f"resumed session {session.session_id} in OBSERVE"
            if resumed
            else f"attached to a new session {session.session_id} in OBSERVE"
        )
        return AttachOutcome(attached=True, detail=detail, session=session, resumed=resumed)

    def _build(self, session: SessionDescriptor) -> _Attached:
        queue = CommandQueue(self.layout, session_id=session.session_id, clock=self.clock)
        reader = JournalReader(self.layout, self.layout.observation_events)
        # Positioned at the end for the same reason a restarted sidecar skips the
        # command stream: observations written before this process existed
        # describe a world it never saw, and replaying them would run the reflex
        # guard against history.
        reader.seek_to_end()
        feed = ObservationFeed(
            layout=self.layout,
            reader=reader,
            # And bound to this session for the same reason the journal is
            # positioned at the end: the slots the pointer names were left there
            # by whatever ran here before, and seeking past them is not an
            # option — the pointer has no position, only a document. The
            # sequence numbers of two sessions are not comparable, so the reader
            # is told whose documents it is allowed to serve.
            snapshots=SnapshotReader(self.layout, session_id=session.session_id),
            max_records=self.limits.observations_per_tick,
        )
        sink = QueueCommandSink(queue, clock=self.clock)
        source = JournalObservationSource(
            feed=feed, store=self._store, clock=self.clock, sleep=self.sleep
        )
        engine = ActionEngine(
            registry=self.registry,
            sink=sink,
            observations=source,
            clock=self.clock,
            panic_stop=self.panic_engaged,
            capability_check=self.capability_check,
            expected_save_id=session.save_id,
            on_dispatch=self._note_dispatch,
        )
        return _Attached(
            session=session,
            queue=queue,
            feed=feed,
            sink=sink,
            source=source,
            engine=engine,
            attached_at_ms=self.clock(),
        )

    def shutdown(self, *, reason: str = "stop requested") -> ShutdownOutcome:
        """Disarm, close in-flight work honestly, and release the lock."""
        # Container notes younger than the memory's flush cadence would die
        # with the process; this is the "and on close" half of that cadence.
        # First, because nothing below depends on it and everything below tears
        # down state it does not need.
        memory = self._container_memory()
        if memory is not None:
            memory.flush_notes(now_ms=self.clock())
        lost: tuple[ActionResult, ...] = ()
        attached = self._attached
        was_armed = self._armed
        self._armed = False
        self._mode = SessionMode.OBSERVE
        pending_arm = self._pending_arm
        if pending_arm is not None:
            self._resolve_arm(
                pending_arm,
                armed=False,
                detail=f"the sidecar shut down before the game confirmed the arm: {reason}",
            )
        if was_armed and attached is not None:
            # The game holds an armed mode this process granted; tell it to
            # drop to OFF. Nothing will tick to read the ack, and that is
            # stated rather than papered over -- but the backstop this named
            # before does not exist. Going stale never disarms the mod:
            # Safety.sidecarStale is read in exactly three places (arm,
            # mayStart, the snapshot) and all three only refuse, while
            # Safety.disarm is reached from a session.disarm command, a new
            # session, or a panic stop. So if this command does not land, the
            # mod keeps reporting the mode it was granted until one of those
            # happens. It cannot act on it -- mayStart refuses everything but
            # stop, disarm and cancel while the heartbeat is stale -- so the
            # residue is a stale reading, not a running agent.
            self._submit_session_disarm(
                attached,
                detail=(
                    "session.disarm sent at shutdown; no tick remains to read the "
                    "ack, and going stale does not disarm the mod by itself"
                ),
                watch=False,
            )
        self._pending_disarm = None
        if self.goals is not None:
            # The queue dies with this process; ending the active goal through
            # its own disarm keeps the record honest for as long as it exists.
            with self.goal_lock:
                self.goals.disarm()
            # And the goal store does not die with it: the disarm's terminal
            # record and the surviving backlog go to disk, so the next sidecar
            # restores a channel that says what this one actually did.
            self._flush_goals()
        if self.actions is not None:
            # Same reasoning one channel over: a submission still waiting when
            # the loop dies must end with a record that says so, because its
            # submitter is still polling the id.
            self.actions.clear_pending(
                ReasonCode.CANCELLED_BY_REQUEST,
                status=ActionStatus.CANCELLED,
                message=f"the sidecar shut down while this submission waited: {reason}",
            )
        if attached is not None:
            closed = attached.queue.close_in_flight(
                ReasonCode.CANCELLED_BY_REQUEST,
                message=f"the sidecar shut down: {reason}",
            )
            lost = () if closed is None else (closed,)
            attached.queue.close()
        released = False
        lock = self.lock
        if lock is not None:
            released = lock.release()
        # The pid record is deliberately left behind: it stops being refreshed,
        # which is what makes ``status`` say "stopped" rather than "never
        # started". Removing it here would erase the difference between a
        # sidecar that ran and one that never did.
        self._attached = None
        return ShutdownOutcome(
            lock_released=released,
            detail=f"shut down: {reason}",
            lost=lost,
        )

    # -- arming ------------------------------------------------------------

    def arm(self, mode: SessionMode = SessionMode.ASSISTED) -> ArmOutcome:
        """Ask for authority to act. Two-phase: nothing here sets ``armed``.

        Refused while the panic sentinel is present and while the game is silent:
        arming against a world nothing is reporting on grants authority over
        nothing. The re-arm requirement §3.12 raises after a save change or a
        game restart is cleared only by the *confirmation* of this explicit call,
        and nowhere else — which is the whole reason it exists as a flag rather
        than as a timeout.

        Two-phase, because of the 2026-08-08 live run: the sidecar flipped its
        own ``armed`` and answered ``armed=true`` while the game — which never
        received a ``session.arm`` command — kept publishing ``mode=OFF``. The
        local gates above are still only the sidecar's opinion; authority over
        the character is the mod's to grant. So this method submits a
        ``session.arm`` command through the ordinary queue (lease, sequencing
        and ack tracking included) and returns ``pending``: across the next
        ticks, :meth:`_watch_pending_arm` waits for the mod's terminal
        ``succeeded`` ack *and* a fresh game heartbeat of this session
        reporting ``armed=true`` in the requested mode, and only both observed
        together set ``armed`` (:meth:`_grant_arm`). A failed ack, a missing
        confirmation or the :attr:`LoopLimits.arm_confirm_timeout_ms` deadline
        resolves ``armed=false`` with the honest reason, published as the
        request's :class:`ControlDecision` and as :attr:`arm_resolution`.
        """
        attached = self._require_attached()
        pending = self._pending_arm
        if pending is not None:
            return ArmOutcome(
                armed=self._armed,
                mode=self._mode,
                detail=(
                    f"an arm into {pending.mode.value} (command {pending.command_id}) "
                    "is already awaiting the game's confirmation; wait for it to resolve"
                ),
                pending=True,
            )
        if mode not in ARMABLE_MODES:
            return ArmOutcome(
                armed=self._armed,
                mode=self._mode,
                detail=f"{mode.value} is not a mode this build arms into",
            )
        if self._armed and self._mode is mode:
            return ArmOutcome(
                armed=True,
                mode=mode,
                detail=f"already armed in {mode.value} on session {attached.session.session_id}",
            )
        if self.panic_engaged():
            return ArmOutcome(
                armed=False,
                mode=self._mode,
                detail="a panic-stop sentinel is present; clear it in the game before arming",
            )
        liveness = self._heartbeats().liveness(Peer.GAME)
        if not liveness.alive:
            return ArmOutcome(
                armed=False,
                mode=self._mode,
                detail=f"the game is not writing a heartbeat ({liveness.detail}); nothing to arm",
            )
        torn = self._journal_refusal()
        if torn is not None:
            return ArmOutcome(armed=False, mode=self._mode, detail=torn)
        refusal = self._multiplayer_refusal()
        if refusal is not None:
            return ArmOutcome(armed=False, mode=self._mode, detail=refusal)
        now = self.clock()
        timeout = self.limits.arm_confirm_timeout_ms
        command = attached.queue.build(
            ActionName.SESSION_ARM,
            idempotency_key=f"session-arm-{uuid.uuid4().hex}",
            # The command is only good for as long as this loop will wait for
            # its confirmation; a session.arm the mod digs up later must not
            # grant a mode nobody is asking for any more.
            lease_ms=timeout,
            args={"mode": mode.value},
        )
        outcome = attached.queue.submit(command)
        if not outcome.accepted:
            rejection = outcome.terminal_result
            return ArmOutcome(
                armed=False,
                mode=self._mode,
                detail=(
                    "session.arm did not reach the command journal: "
                    f"{'the queue refused it' if rejection is None else rejection.message}"
                ),
            )
        self._pending_arm = PendingArm(
            command_id=outcome.command.command_id,
            idempotency_key=outcome.command.idempotency_key,
            mode=mode,
            session_id=attached.session.session_id,
            requested_at_ms=now,
            deadline_ms=now + timeout,
        )
        return ArmOutcome(
            armed=False,
            mode=self._mode,
            detail=(
                f"session.arm {outcome.command.command_id} submitted for {mode.value}; "
                f"awaiting the game's terminal ack and a confirming heartbeat "
                f"(within {timeout} ms)"
            ),
            pending=True,
        )

    # -- the confirmation the arm waits for --------------------------------

    def _watch_pending_arm(
        self, attached: _Attached, now_ms: int, *, panic: bool, game_alive: bool
    ) -> None:
        """Advance the two-phase arm by what this tick observed; see :meth:`arm`.

        The terminal ack is read from the queue's idempotency cache rather
        than from a private ack reader: whichever drain consumed it — this
        tick's poll or the engine's, mid-command — the cache holds the first
        terminal result filed under the command's key, and a second reader
        over the ack journal is exactly what :class:`QueueCommandSink` forbids.
        """
        pending = self._pending_arm
        if pending is None:
            return
        if panic:
            self._resolve_arm(
                pending,
                armed=False,
                detail=(
                    "a panic-stop sentinel appeared while the arm awaited the game's "
                    "confirmation; clear it in the game and ask again"
                ),
            )
            return
        if not game_alive:
            self._resolve_arm(
                pending,
                armed=False,
                detail=(
                    "the game stopped writing a heartbeat while the arm awaited its "
                    "confirmation; nothing to arm"
                ),
            )
            return
        if not pending.acked:
            ack = attached.queue.cache.replay(pending.idempotency_key)
            if ack is not None:
                if ack.status is not ActionStatus.SUCCEEDED:
                    self._resolve_arm(
                        pending,
                        armed=False,
                        detail=(
                            f"the game refused session.arm ({ack.reason_code.value}): "
                            f"{ack.message or 'no detail was given'}"
                        ),
                    )
                    return
                pending = replace(pending, acked=True)
                self._pending_arm = pending
        if pending.acked and self._arm_confirmed_by_heartbeat(pending):
            self._grant_arm(attached, pending)
            return
        if now_ms < pending.deadline_ms:
            return
        timeout = self.limits.arm_confirm_timeout_ms
        if pending.acked:
            detail = (
                f"the game acked session.arm ({pending.command_id}) but no heartbeat "
                f"confirming armed=true in {pending.mode.value} arrived within "
                f"{timeout} ms; the sidecar stays disarmed"
            )
        else:
            detail = (
                f"the game never confirmed the arm within {timeout} ms: no terminal "
                f"ack for session.arm ({pending.command_id}) arrived; the sidecar "
                "stays disarmed"
            )
        self._resolve_arm(pending, armed=False, detail=detail)
        # Countermand: an ack that lands *after* this deadline must not leave
        # the game armed in a mode this loop never granted, so the abandoned
        # arm is followed by a disarm the mod honours unconditionally
        # (session.disarm bypasses its arming gate by design).
        self._submit_session_disarm(
            attached,
            detail="session.disarm sent to countermand an arm the game never confirmed",
            watch=True,
        )

    def _arm_confirmed_by_heartbeat(self, pending: PendingArm) -> bool:
        """Whether the game's *own* heartbeat reports the requested authority.

        Same session, written no earlier than the request, ``armed=true`` and
        the exact requested mode. The freshness bound is what stops a stale
        armed heartbeat — one left over from before a crash — from confirming
        an arm the current game process never granted.
        """
        liveness = self._heartbeats().liveness(Peer.GAME)
        beat = liveness.heartbeat
        return (
            liveness.alive
            and beat is not None
            and beat.session_id == pending.session_id
            and beat.timestamp_ms >= pending.requested_at_ms
            and beat.armed is True
            and beat.mode is pending.mode
        )

    def _grant_arm(self, attached: _Attached, pending: PendingArm) -> None:
        """Both halves of the confirmation were observed; authority is real now.

        Everything the one-phase arm used to do lands here instead: the §3.12
        re-arm flag clears, the goal channel arms, the sidecar heartbeat says
        so — each of them only ever downstream of the game's own word.
        """
        self._session_manager().mark_rearmed()
        self._mode = pending.mode
        self._armed = True
        if self.goals is not None:
            # The channel arms and disarms with the session: activation is an
            # authority, and the queue's own NOT_ARMED refusal is what answers
            # an activation nothing granted.
            with self.goal_lock:
                self.goals.arm()
        self._publish_heartbeat()
        self._resolve_arm(
            pending,
            armed=True,
            detail=(
                f"armed in {pending.mode.value} on session {pending.session_id}: the "
                f"game acked session.arm ({pending.command_id}) and its heartbeat "
                f"reports armed=true in {pending.mode.value}"
            ),
        )

    def _resolve_arm(self, pending: PendingArm, *, armed: bool, detail: str) -> None:
        """End the pending arm with its observed outcome, everywhere it is read.

        The :class:`ControlDecision` for the request that asked (when one did)
        is published here and only here — the control waiter must never see a
        decision for an arm whose two-phase outcome is not known yet.
        """
        self._pending_arm = None
        self._arm_resolution = ArmOutcome(
            armed=armed, mode=self._mode, detail=detail, changed=armed
        )
        if pending.nonce is not None:
            self._control_decision = ControlDecision(
                nonce=pending.nonce,
                kind=ControlKind.ARM,
                armed=self._armed,
                mode=self._mode,
                detail=detail,
                applied=armed,
            )

    def _journal_refusal(self) -> str | None:
        """Why this session may not be armed, when a damaged journal is the reason.

        E12-M03-T005: a truncated journal is detected and the session refuses
        to arm. A stream that ends mid-record has lost its last record — a
        producer died mid-write, or something cut the file — and every judgement
        an armed session makes (acks consumed, observations folded in, commands
        sequenced) reads these streams. Granting authority over a stream whose
        most recent record is known to be missing would act on a world one
        record behind the one the mod described. The refusal is recoverable and
        says how: the tear heals when the producer finishes the line or the
        stream is rewritten, and disarming is never gated on it.
        """
        for stream in self.layout.journals():
            problem = probe_truncation(stream)
            if problem is not None:
                return (
                    f"the exchange journal is damaged ({problem}); arming over a "
                    "truncated stream is refused. Restart the game so the mod "
                    "rewrites its journals, then ask again"
                )
        return None

    def _multiplayer_refusal(self) -> str | None:
        """Why this session may not be armed, when multiplayer is the reason.

        The blueprint forbids multiplayer outright, and until now nothing
        enforced it. ``safety.allow_multiplayer`` produced a warning that said
        "multiplayer is refused at the handshake regardless of this setting"
        while no such refusal existed anywhere in the mod or the sidecar — a
        setting that read like a bypass and was one.

        Three states, and only one of them arms. ``False`` is the mod saying it
        looked and this is single player. ``True`` is a refusal. **``None`` is
        also a refusal**, because an absent reading is not a negative reading —
        the same rule that stops a missing ``is_bleeding`` from meaning "not
        bleeding". There is no flag that turns this off; a gate with an
        override is not a gate.
        """
        current = self._store.latest()
        if current is None:
            # Nothing has been reported yet, which is a normal moment to arm in:
            # attach, arm, then act on the first snapshot. Nothing is lost by
            # allowing it, because ActionEngine._multiplayer_abort re-decides
            # this for every command against the observation it is acting on —
            # which is the check that actually protects the session.
            return None
        if current.game.multiplayer is True:
            return (
                "this is a multiplayer session, which this build refuses to act in. "
                "There is no setting that permits it."
            )
        if current.game.multiplayer is None:
            return (
                "the mod did not report whether this is a multiplayer session, and an "
                "absent reading is not a negative one. Arming is refused. If the game "
                "is single player, this means the mod could not read isClient/isServer "
                "on this build — see docs/GAME_API_VERIFICATION.md."
            )
        return None

    def disarm(self, *, reason: str = "requested") -> ArmOutcome:
        """Drop back to ``OBSERVE``. Always succeeds; disarming is never gated.

        The goal channel disarms through its own lever: whatever forced this —
        a user request, a reflex guard event, a silent game — the active goal
        ends in the queue's own vocabulary (``CANCELLED`` / ``NOT_ARMED``,
        E08-M02-T007) and the backlog stays, exactly as
        :meth:`~pz_agent_core.goals.GoalQueue.disarm` documents.

        The game is told, without being waited for. ``session.disarm`` exists
        end-to-end (the mod's ``DisarmAdapter`` drops its Safety state to
        ``OFF`` and acks with the observed before/after), so when this disarm
        actually revoked authority a ``session.disarm`` command follows it —
        the mirror of the two-phase arm, minus the gate: the local disarm
        takes effect *first* and unconditionally, because refusing to drop
        authority is never right, and the game's answer is then watched the
        same bounded way and reported on :attr:`disarm_notice` rather than
        allowed to un-disarm anything. A disarm that revoked nothing *and*
        superseded no pending arm sends nothing: the panic level calls this
        every tick it holds, and a command per tick would be the unbounded
        stream this project forbids.
        """
        changed = self._armed or self._mode is not SessionMode.OBSERVE
        pending_arm = self._pending_arm
        if pending_arm is not None:
            self._resolve_arm(
                pending_arm,
                armed=False,
                detail=f"a disarm ({reason}) superseded the arm awaiting the game's confirmation",
            )
        self._armed = False
        self._mode = SessionMode.OBSERVE
        if self.goals is not None:
            with self.goal_lock:
                self.goals.disarm()
        if self.actions is not None:
            # The action channel has no backlog worth keeping across a disarm:
            # unlike a goal, a queued action is an imperative about the world
            # right now, and running it whenever authority next returns would
            # act on an intent nobody re-stated. It ends, and the record names
            # the lever that ended it.
            self.actions.clear_pending(
                ReasonCode.NOT_ARMED,
                status=ActionStatus.REJECTED,
                message=f"the sidecar disarmed while this submission waited: {reason}",
            )
        if self._attached is not None:
            if changed or pending_arm is not None:
                # The superseded arm is the second thing worth countermanding.
                # It is still on the command journal and still inside its lease,
                # and the mod's arm gate refuses only a stale sidecar heartbeat
                # — which this beating sidecar does not have — so it may yet
                # grant a mode this loop has just abandoned, exactly as a late
                # ack does past the confirmation deadline (_watch_pending_arm
                # countermands that one for the same reason). Bounded: the
                # pending arm is resolved above, so the tick-by-tick disarm a
                # held panic sentinel performs sends this once, not per tick.
                self._submit_session_disarm(
                    self._attached,
                    detail=(
                        f"session.disarm sent after disarming locally ({reason})"
                        if changed
                        else (
                            "session.disarm sent to countermand the arm this disarm "
                            f"superseded, which the game may still grant ({reason})"
                        )
                    ),
                    watch=True,
                )
            self._publish_heartbeat()
        return ArmOutcome(
            armed=False,
            mode=SessionMode.OBSERVE,
            detail=f"disarmed: {reason}",
            changed=changed,
        )

    def _submit_session_disarm(self, attached: _Attached, *, detail: str, watch: bool) -> None:
        """Ship one ``session.disarm`` and, when a tick remains to read it, watch it.

        Never raises and never gates anything: the local disarm this follows
        has already happened, and a command journal that cannot take the
        record is reported on :attr:`disarm_notice` as unconfirmed rather than
        allowed to matter to the revocation.
        """
        timeout = self.limits.arm_confirm_timeout_ms
        command = attached.queue.build(
            ActionName.SESSION_DISARM,
            idempotency_key=f"session-disarm-{uuid.uuid4().hex}",
            lease_ms=timeout,
        )
        outcome = attached.queue.submit(command)
        if not outcome.accepted:
            rejection = outcome.terminal_result
            self._pending_disarm = None
            self._disarm_notice = DisarmNotice(
                command_id=command.command_id,
                confirmed=False,
                detail=(
                    "session.disarm did not reach the command journal: "
                    f"{'the queue refused it' if rejection is None else rejection.message}"
                ),
            )
            return
        now = self.clock()
        self._disarm_notice = DisarmNotice(
            command_id=outcome.command.command_id,
            confirmed=False,
            detail=detail,
        )
        self._pending_disarm = (
            PendingArm(
                command_id=outcome.command.command_id,
                idempotency_key=outcome.command.idempotency_key,
                mode=SessionMode.OBSERVE,
                session_id=attached.session.session_id,
                requested_at_ms=now,
                deadline_ms=now + timeout,
            )
            if watch
            else None
        )

    def _watch_pending_disarm(self, attached: _Attached, now_ms: int) -> None:
        """Fold the game's answer to ``session.disarm`` into :attr:`disarm_notice`.

        The mod's ack is its own observed postcondition (the ``DisarmAdapter``
        reports ``armed_after`` / ``mode_after``), so the ack alone settles
        this — unlike the arm, no heartbeat is required, because nothing here
        is waiting to *grant* anything on the strength of the answer.
        """
        pending = self._pending_disarm
        if pending is None:
            return
        ack = attached.queue.cache.replay(pending.idempotency_key)
        if ack is not None:
            self._pending_disarm = None
            if ack.status is ActionStatus.SUCCEEDED:
                self._disarm_notice = DisarmNotice(
                    command_id=pending.command_id,
                    confirmed=True,
                    detail=(
                        "the game confirmed session.disarm "
                        f"(armed_after={ack.evidence.get('armed_after')}, "
                        f"mode_after={ack.evidence.get('mode_after')})"
                    ),
                )
            else:
                self._disarm_notice = DisarmNotice(
                    command_id=pending.command_id,
                    confirmed=False,
                    detail=(
                        f"the game answered session.disarm with {ack.status.value} "
                        f"({ack.reason_code.value}): {ack.message or 'no detail was given'}"
                    ),
                )
            return
        if now_ms >= pending.deadline_ms:
            self._pending_disarm = None
            self._disarm_notice = DisarmNotice(
                command_id=pending.command_id,
                confirmed=False,
                detail=(
                    "the game never acked session.disarm within "
                    f"{self.limits.arm_confirm_timeout_ms} ms; its own takeover and "
                    "panic safety are the backstop, and a stale heartbeat stops it "
                    "starting anything, though it does not disarm it"
                ),
            )

    def panic_engaged(self) -> bool:
        """True while the mod's panic sentinel is in the exchange directory."""
        return self.layout.panic_stop.is_file()

    # -- one tick ----------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Read the world, run the guard, and only then consider acting."""
        attached = self._require_attached()
        self._tick += 1
        self._fresh_problems = []
        now = self.clock()
        panic = self.panic_engaged()
        liveness = self._heartbeats().liveness(Peer.GAME, now)
        ingested = attached.feed.drain(self._store)
        # Drained here as well as inside the engine so that a terminal ack for a
        # command nobody is driving any more still frees the backpressure slot.
        attached.sink.poll_acks()
        if self.actions is not None:
            # Before anything is served: a submission past its lease must time
            # out, not be dispatched against a world its caller gave up on.
            self.actions.sweep(now)

        self._trace_observation()

        # The container-memory seam, sighting half: whatever world containers
        # this tick's observation shows are offered to the save's memory. Before
        # the guard and unconditionally — remembering where a shelf stands is
        # passive, and it must keep happening while the loop is disarmed, which
        # is most of every session. The memory batches its own disk writes.
        memory = self._container_memory()
        if memory is not None and ingested:
            latest = self._store.latest()
            if latest is not None:
                memory.note_observation(latest, now_ms=now)

        events = self._run_guard(attached, now, panic=panic, game_alive=liveness.alive)
        lost = self._apply_events(attached, events, now, panic=panic, game_alive=liveness.alive)
        self._trace_results(lost)
        disarmed_by_guard = any(event.forces_disarm for event in events) or panic

        # The two-phase arm and the disarm notice advance on what this tick
        # observed — after the stop levers above, so a panic or a silent game
        # resolves a waiting arm before anything could grant it.
        self._watch_pending_arm(attached, now, panic=panic, game_alive=liveness.alive)
        self._watch_pending_disarm(attached, now)

        control = self._consume_control(now, attached.attached_at_ms)
        stop = self._apply_control(control, events)
        # After the stop levers, before any acting: cancellations, wall-clock
        # budgets and pending time-to-lives are applied by the queue's own
        # tick, so a goal ends for real rather than when somebody asks.
        self._tick_goals()
        # The remote submission first, then the planner: an action a client
        # asked for by name outranks the loop's own initiative, and both pass
        # the same gates and spend the same budget.
        served = self._serve_remote_action(
            attached, now, events=events, panic=panic, game_alive=liveness.alive
        )
        results = served + self._act(
            attached, now, events=events, panic=panic, game_alive=liveness.alive
        )
        self._trace_results(results)

        # The container-memory seam, inspection half: a terminal result whose
        # evidence proves a container's contents were observed teaches the
        # memory what an enumeration found. After the results settle, so the
        # observation consulted is the one the engine's own verify read or a
        # newer one. Bounded by the results themselves — at most two per tick.
        if memory is not None and results:
            self._feed_inspections(memory, results, now)

        # The goal-persistence seam: whatever this tick (or the serving thread
        # since the last one) did to the channel reaches disk before the tick
        # ends. Skipped without IO when no goal moved; see _flush_goals.
        self._flush_goals()

        self._publish_heartbeat()
        self._refresh_pid_record()
        return TickOutcome(
            tick=self._tick,
            now_ms=now,
            mode=self._mode,
            armed=self._armed,
            game_alive=liveness.alive,
            panic=panic,
            ingested=ingested,
            events=tuple(events),
            lost=lost,
            results=results,
            disarmed=disarmed_by_guard,
            stop=stop,
            detail=liveness.detail,
            state_problems=tuple(self._fresh_problems),
        )

    def _note_state_problem(self, problem: str) -> None:
        """Record one failed state-directory write, for this tick and the ring."""
        self._fresh_problems.append(problem)
        self._state_problems.append(problem)

    def _refresh_pid_record(self) -> None:
        """Prove this process is still ticking, without dying when it cannot.

        E12-M03-T004: the state directory becoming unwritable mid-session is
        reported rather than fatal. The session outranks its own status record
        — raising here would end a session that is driving a character over a
        file whose only job is to describe that session — but silence would be
        the opposite lie, because an unrefreshed pid record reads as a stopped
        sidecar to ``pz-agent status`` and to a second ``start``.
        """
        if self.pid_file is None:
            return
        try:
            self.pid_file.refresh()
        except OSError as exc:
            self._note_state_problem(
                f"the pid record could not be refreshed ({exc.strerror or exc}); the "
                "state directory is not accepting writes, and until it does "
                "'pz-agent status' will read this still-running sidecar as stopped"
            )

    def _container_memory(self) -> ContainerMemory | None:
        """The feedable save memory behind this loop's planner, if there is one.

        Walked structurally — ``planner``, then each wrapper's ``inner`` — and
        matched against :class:`ContainerMemory`, because the concrete planner
        and wrapper classes import this module and cannot be imported back.
        The walk is the same one ``unwrap_planner`` performs on the assembly
        side, bounded here by :data:`_MAX_PLANNER_UNWRAPS`. None is the
        ordinary answer for a loop assembled without a planner, or with a
        planner whose memory is one of the storeless fallbacks.
        """
        candidate: object | None = self.planner
        for _ in range(_MAX_PLANNER_UNWRAPS):
            if candidate is None:
                return None
            memory = getattr(candidate, "memory", None)
            if isinstance(memory, ContainerMemory):
                return memory
            candidate = getattr(candidate, "inner", None)
        return None

    def _feed_inspections(
        self, memory: ContainerMemory, results: Sequence[ActionResult], now_ms: int
    ) -> None:
        """Offer each result's content evidence to the save memory.

        Terminal status is deliberately not checked: a batch transfer that
        stopped at a full destination ends FAILED while its evidence still
        records what was observed landing there, and observed content is worth
        remembering whichever way the action ended. What *is* required is the
        evidence shape the engine attaches (``kind`` beside ``observed``) with
        a kind in :data:`_CONTENT_EVIDENCE_REFS`; anything else — a refusal's
        flat evidence, another adapter's kind — is simply not content.
        """
        latest = self._store.latest()
        if latest is None:
            return
        for result in results:
            kind = result.evidence.get("kind")
            if not isinstance(kind, str):
                continue
            ref_key = _CONTENT_EVIDENCE_REFS.get(kind)
            if ref_key is None:
                continue
            observed = result.evidence.get("observed")
            if not isinstance(observed, Mapping):
                continue
            container_ref = observed.get(ref_key)
            if isinstance(container_ref, str) and container_ref:
                memory.note_inspection(container_ref, latest, now_ms=now_ms)

    def _run_guard(
        self, attached: _Attached, now_ms: int, *, panic: bool, game_alive: bool
    ) -> list[SafetyEvent]:
        """Evaluate the deterministic guard. Called before anything is composed.

        With no observation yet there is nothing to evaluate *against*, and the
        guard is a pure function of two observations — so the honest answer is an
        empty list, not a fabricated one built from defaults.
        """
        current = self._store.latest()
        if current is None:
            return []
        signals = ReflexSignals(
            now_ms=now_ms,
            panic_requested=panic,
            game_alive=game_alive,
            sidecar_alive=True,
            in_flight=self._in_flight(attached),
        )
        events = self.guard.evaluate(self._store.previous(), current, signals)
        self._events.extend(events)
        return events

    def _in_flight(self, attached: _Attached) -> tuple[InFlightCommand, ...]:
        return tuple(
            InFlightCommand(
                command_id=command.command_id,
                action=command.action.value,
                deadline_ms=command.deadline_ms(),
                last_progress_ms=attached.sink.last_progress_ms(command),
                moves_character=command.action in _MOVING_ACTIONS,
            )
            for command in attached.queue.pending
        )

    def _apply_events(
        self,
        attached: _Attached,
        events: Sequence[SafetyEvent],
        now_ms: int,
        *,
        panic: bool,
        game_alive: bool,
    ) -> tuple[ActionResult, ...]:
        """Carry out what the guard authorised, plus the two file-level facts.

        The guard cannot see a heartbeat or a sentinel — both reach it as already
        decided booleans — so the loop still owns closing work when the game goes
        silent and when the panic file appears. Both close as ``lost``: the mod
        may well have finished the action before it went away, and asserting
        either outcome would be a claim about a world nobody looked at.
        """
        lost: list[ActionResult] = []
        if not game_alive:
            closed = attached.queue.close_in_flight(
                ReasonCode.GAME_DISCONNECTED,
                message="the game stopped writing a heartbeat while this command was running",
            )
            if closed is not None:
                lost.append(closed)
        if panic:
            closed = attached.queue.close_in_flight(
                ReasonCode.PANIC_STOP,
                message="a panic stop was requested while this command was running",
            )
            if closed is not None:
                lost.append(closed)
        for event in events:
            if not event.command_ids:
                continue
            in_flight = attached.queue.in_flight
            if in_flight is not None and in_flight.command_id in event.command_ids:
                closed = attached.queue.close_in_flight(event.reason_code, message=event.message)
                if closed is not None:
                    lost.append(closed)
        if any(e.reason_code is ReasonCode.USER_TAKEOVER for e in events):
            # User input always wins, and a goal is the loop's largest unit of
            # initiative — but "wins" must not mean "silently discards": the
            # active goal is parked as paused-by-user *before* the cancel that
            # ends it in the queue, so status keeps saying what happened to it.
            self._pause_active_goal(now_ms, reason="manual takeover")
        forcing = sorted({e.reason_code.value for e in events if e.forces_disarm})
        if panic:
            # §8.1/§8.12 and E08-M02-T008: a panic stop leaves *nothing* in
            # flight in the goal channel — no active goal, no backlog, no
            # pending cancel — through the queue's own lever, before the disarm
            # below (whose queue-side disarm is then a no-op). A level, not an
            # edge: while the sentinel stays, every tick re-clears whatever
            # arrived in the meantime.
            self._panic_goals()
            self._panic_actions()
            self.disarm(reason="panic stop")
        elif forcing:
            self.disarm(reason=", ".join(forcing))
        elif not game_alive and self._armed:
            self.disarm(reason=ReasonCode.GAME_DISCONNECTED.value)
        return tuple(lost)

    def _consume_control(self, now_ms: int, attached_at_ms: int) -> ControlRequest | None:
        """Take at most one pending request, refusing the ones that prove nothing.

        An **arm** issued before this process attached is refused outright, and
        so is one that has gone stale — or one stamped so far ahead of this tick
        that it cannot have been written against this clock. That is the
        mechanism behind "never
        re-arms itself": after a crash, the ``arm`` file the user wrote for the
        *previous* session is still on disk, and consuming it would hand
        authority to a loop that has just come up knowing nothing about the
        world. The file is cleared either way — an unconsumable request left in
        place would be re-judged on every tick forever.

        Neither test applies to a **stop** or a **disarm**. Both only ever take
        authority away, so there is no authority a late one could grant, and the
        two refusals cost opposite things: honouring a stale stop costs a
        restart, while refusing one leaves the agent armed after the user has
        been told it stopped. Late is also the ordinary case rather than the
        exotic one — this channel is read between ticks, and a single tick that
        drives ``literature.read`` occupies the loop for that adapter's whole
        two-minute budget, so a request typed seconds into it is already older
        than :data:`CONTROL_MAX_AGE_MS` by the time this method sees it. The mod
        makes the same exemption for the same reason: ``safety.stop``,
        ``session.disarm`` and ``plan.cancel`` bypass its lease check.
        """
        channel = self._control_channel()
        request = channel.read()
        if request is None:
            return None
        try:
            channel.clear()
        except OSError as exc:
            # Discarding the unlink failure itself: the state directory has
            # stopped accepting writes (E12-M03-T004), and ending the session
            # over an unremovable request file would be the larger loss. The
            # request is still applied this tick; a leftover file is re-judged
            # next tick, where the staleness rules below bound what a
            # non-consumable arm can do (a stop or disarm only ever takes
            # authority away), and the condition is reported.
            self._note_state_problem(
                f"the control request file could not be removed ({exc.strerror or exc}); "
                "the state directory is not accepting writes, so this request may be "
                "seen again on later ticks"
            )
        if request.kind is not ControlKind.ARM:
            return request
        if request.issued_at_ms < attached_at_ms:
            self._decide(
                request,
                applied=False,
                detail=(
                    "the arm request was issued before this sidecar attached, so it is "
                    "refused; a request left behind by a previous run proves nothing — ask again"
                ),
            )
            return None
        if request.issued_at_ms - now_ms > CONTROL_MAX_AGE_MS:
            # The other end of the same bound, and it is not symmetry for its own
            # sake: both tests above subtract two *wall*-clock readings taken in
            # different processes, so a run that wrote its request while the
            # machine's clock was ahead — before NTP corrected it, or after the
            # user set it by hand — leaves a file the attach test reads as
            # "issued after we came up" and the age test reads as "negative, so
            # fresh". That is a previous run's arm arming this one. A request
            # stamped a little ahead is ordinary (this tick's ``now_ms`` was read
            # before the request was written, and the writer is another process),
            # so the tolerance is the same ceiling rather than zero; past it the
            # stamp is not a measurement of anything this loop can compare with.
            self._decide(
                request,
                applied=False,
                detail=(
                    f"the arm request is stamped more than {CONTROL_MAX_AGE_MS} ms ahead "
                    "of this tick, so it was written against a different clock and its "
                    "age cannot be measured; it proves nothing about what the user wants "
                    "now — check the system clock and ask again"
                ),
            )
            return None
        if now_ms - request.issued_at_ms > CONTROL_MAX_AGE_MS:
            self._decide(
                request,
                applied=False,
                detail=(
                    f"the arm request is older than {CONTROL_MAX_AGE_MS} ms and no longer "
                    "proves what the user wants now; ask again"
                ),
            )
            return None
        return request

    def _apply_control(
        self, request: ControlRequest | None, events: Sequence[SafetyEvent]
    ) -> StopCause | None:
        """Apply one request. Stopping and disarming are never refused; arming can be.

        An arm that lands on the same tick as an event demanding a disarm is
        refused rather than applied and undone: the user asked before the guard
        had seen what it has now seen, and the safe reading of that race is that
        they have not asked yet.
        """
        if request is None:
            return None
        if request.kind is ControlKind.STOP:
            self._decide(request, applied=True, detail="stop requested; the loop is shutting down")
            return StopCause.STOP_REQUESTED
        if request.kind is ControlKind.DISARM:
            outcome = self.disarm(reason="requested")
            self._decide(request, applied=True, detail=outcome.detail)
            return None
        if any(event.forces_disarm for event in events):
            self._decide(
                request,
                applied=False,
                detail=(
                    "the guard demanded a disarm on the same tick, so the arm request is "
                    "refused; see what it saw with 'pz-agent status', then ask again"
                ),
            )
            return None
        wanted = request.mode or SessionMode.ASSISTED
        outcome = self.arm(wanted)
        pending = self._pending_arm
        if outcome.pending and pending is not None:
            if pending.nonce is None and pending.mode is wanted:
                # No decision yet, on purpose: the waiter polling for this
                # nonce must read the two-phase outcome, not the submission.
                # The nonce rides on the pending arm and :meth:`_resolve_arm`
                # publishes the decision once the confirmation — or its
                # refusal — is observed.
                self._pending_arm = replace(pending, nonce=request.nonce)
                return None
            # The pending arm answers to somebody else (an earlier request's
            # nonce, or a different mode a direct caller asked for): this
            # request is refused now, in the loop's own words.
            self._decide(request, applied=False, detail=outcome.detail)
            return None
        self._decide(request, applied=outcome.armed, detail=outcome.detail)
        return None

    def _decide(self, request: ControlRequest, *, applied: bool, detail: str) -> None:
        """Publish what this tick did with *request*; see :class:`ControlDecision`."""
        self._control_decision = ControlDecision(
            nonce=request.nonce,
            kind=request.kind,
            armed=self._armed,
            mode=self._mode,
            detail=detail,
            applied=applied,
        )

    def _act(
        self,
        attached: _Attached,
        now_ms: int,
        *,
        events: Sequence[SafetyEvent],
        panic: bool,
        game_alive: bool,
    ) -> tuple[ActionResult, ...]:
        """Let the planner propose one action, if everything permits it.

        Every gate here is a refusal to start work, never a way to stop work
        already running — that is what :meth:`_apply_events` did, above, before
        this method was reached.

        The typed goal channel is served here and only here: armed in
        ``AUTONOMOUS`` with a :class:`GoalPlanner`, the loop activates the
        oldest admissible goal when none is active and asks the planner for a
        plan *for that goal* — the same join
        ``tests/contract/test_goal_reaches_the_planner.py`` proves on the core
        side, made true of the running loop. With no goal to serve, the
        planner's own initiative proposes exactly as before.
        """
        planner = self.planner
        if planner is None or not self._armed:
            return ()
        if panic or not game_alive or events:
            return ()
        current = self._store.latest()
        if current is None or not current.can_act:
            return ()
        if not self._budget.allows(now_ms):
            return ()
        request: ActionRequest | None
        goal: GoalRecord | None = None
        if (
            self.goals is not None
            and self._mode is SessionMode.AUTONOMOUS
            and isinstance(planner, GoalPlanner)
        ):
            goal = self._goal_to_serve()
        if goal is not None:
            assert isinstance(planner, GoalPlanner)  # narrowed above; restated for the type
            request = planner.propose_for_goal(to_planner_goal(goal), current)
        else:
            request = planner.propose(current)
        if request is None:
            return ()
        self._budget.spend(now_ms)
        result = attached.engine.execute(request)
        self._confirm(request.action, result)
        if goal is not None:
            self._settle_goal_step(goal.goal_id, result)
        return (result,)

    # -- the remote action channel -----------------------------------------

    def _serve_remote_action(
        self,
        attached: _Attached,
        now_ms: int,
        *,
        events: Sequence[SafetyEvent],
        panic: bool,
        game_alive: bool,
    ) -> tuple[ActionResult, ...]:
        """Drain at most one remote submission through the local action funnel.

        The gates are :meth:`_act`'s, applied in the same order and for the
        same reasons — the one difference is what a closed gate does. A planner
        that is not consulted simply proposes nothing; a submission is real
        work somebody is polling an id for, so the two gates that will not open
        again on their own end it with a record that says why: no authority is
        ``NOT_ARMED`` (a level — a submission that arrives while disarmed is
        ended on the next tick, not parked until authority someday returns and
        a stale intent runs), and the panic sentinel already emptied the queue
        in :meth:`_apply_events`. The transient gates — a silent game the loop
        is still deciding about, a guard event, a missing observation, a spent
        budget — leave the submission waiting for a later tick, bounded by the
        levers themselves: whatever holds those gates closed for long either
        disarms the loop or ends the run.

        The dispatch is the same one a planner proposal gets: one budget slot
        spent, :meth:`~pz_agent_core.actions.engine.ActionEngine.execute`
        driven to its own terminal result on this tick thread — capability
        check, permission machinery, observed postcondition and all — and that
        result recorded whole. Nothing about the outcome is decided here.
        """
        channel = self.actions
        if channel is None or not channel.has_pending:
            return ()
        if not self._armed:
            channel.clear_pending(
                ReasonCode.NOT_ARMED,
                status=ActionStatus.REJECTED,
                message=(
                    "the sidecar is not armed; arm it ('pz-agent arm' or session.arm) "
                    "and submit again"
                ),
            )
            return ()
        if panic or not game_alive or events:
            return ()
        current = self._store.latest()
        if current is None or not current.can_act:
            return ()
        if not self._budget.allows(now_ms):
            return ()
        taken = channel.take_next()
        if taken is None:
            return ()
        action_id, request = taken
        self._budget.spend(now_ms)
        result = attached.engine.execute(request)
        channel.settle(action_id, result)
        self._confirm(request.action, result)
        return (result,)

    # -- the typed goal channel --------------------------------------------

    def _goal_to_serve(self) -> GoalRecord | None:
        """The active goal, activating the oldest admissible one when none is.

        Reached only armed, in ``AUTONOMOUS``, with a goal-capable planner.
        Every refusal :meth:`~pz_agent_core.goals.GoalQueue.activate_next` can
        answer — a disarmed channel, every waiting goal already cancelled —
        means the same thing here: nothing to serve this tick, so the planner's
        own initiative is consulted instead and the queue's next tick reports
        what the refusal already knew.
        """
        queue = self.goals
        if queue is None:
            return None
        with self.goal_lock:
            active = queue.active
            if active is not None:
                return active
            if not queue.pending:
                return None
            activated = queue.activate_next().goal
        if activated is not None and self._paused_goal is not None:
            # A fresh activation is the explicit resumption path: the user
            # armed again and stated a goal, so the parked marker has served
            # its purpose. Nothing before this point clears it — a bare re-arm
            # keeps it visible on purpose.
            self._paused_goal = None
        return activated

    def _pause_active_goal(self, now_ms: int, *, reason: str) -> None:
        """Park the active goal as paused-by-user and ask the queue to end it.

        The queue's own cancel is the only lever used — the goals package has
        no ``PAUSED`` state, so the record honestly ends ``CANCELLED`` on a
        following tick and the loop's :attr:`paused_goal` marker carries the
        "paused, not abandoned" half until an explicit new activation replaces
        it. Nothing implicit resumes it; see :class:`PausedGoal`.
        """
        queue = self.goals
        if queue is None:
            return
        with self.goal_lock:
            active = queue.active
            if active is None:
                return
            self._paused_goal = PausedGoal(
                goal_id=active.goal_id,
                kind=active.kind.value,
                reason=reason,
                paused_at_ms=now_ms,
            )
            queue.request_cancel(active.goal_id)

    def _settle_goal_step(self, goal_id: str, result: ActionResult) -> None:
        """Fold one action's terminal result into the goal it served.

        A success with observed postcondition evidence ends the goal through
        :meth:`~pz_agent_core.goals.GoalQueue.succeed` — the one route to a
        ``SUCCEEDED`` goal, and it refuses without evidence. Everything else
        spends one step through
        :meth:`~pz_agent_core.goals.GoalQueue.note_step`, which applies an
        outstanding cancellation first and expires the goal when that was its
        last step. Both answers are the queue's own; nothing here invents one.
        """
        queue = self.goals
        if queue is None:
            return
        with self.goal_lock:
            if result.status is ActionStatus.SUCCEEDED and result.evidence:
                queue.succeed(goal_id, result)
            else:
                queue.note_step(goal_id)

    def _tick_goals(self) -> None:
        """Run the queue's own tick, so every bound expires whether or not asked."""
        queue = self.goals
        if queue is None:
            return
        with self.goal_lock:
            queue.tick()

    def _panic_goals(self) -> None:
        """Empty the channel through its own panic lever; see :meth:`_apply_events`."""
        queue = self.goals
        if queue is None:
            return
        with self.goal_lock:
            queue.panic_stop()

    def adopt_goal_store(self, store: GoalStore) -> tuple[str, ...]:
        """Restore the persisted goal channel into this loop's fresh queue, and
        keep it persisted from here on.

        Called once at assembly, after construction and before the first tick —
        the queue is fresh and nothing else is running, so the restore is
        race-free; the ``goal_lock`` is still taken because the discipline is
        cheaper to keep than to reason about an exception for. What the restore
        does with each record is the queue's own honest reading
        (:meth:`~pz_agent_core.goals.GoalQueue.restore`): the previous ACTIVE
        goal becomes terminal ``FAILED`` / ``SESSION_TERMINATED``, the pending
        backlog comes back pending under its original ids, digests and
        submission times, and the terminal history comes back so an old goal id
        still answers over ``pz_goal_status``.

        A file that cannot be read or is not a channel this build wrote is
        never guessed at: the diagnostic goes to :attr:`state_problems`, the
        file is set aside as ``goals.json.corrupt`` so the evidence survives,
        and the channel starts empty. Returns the notes it recorded plus a
        summary of what was restored, for the caller that wants to log them.
        """
        queue = self.goals
        if queue is None:
            # No channel, nothing to persist; the store is not adopted, so the
            # loop never writes a file claiming to describe goals it cannot hold.
            return ("this sidecar was assembled without a goal queue, so no goals persist",)
        self.goal_store = store
        notes: list[str] = []
        snapshot = None
        try:
            snapshot = store.load()
        except (GoalStoreError, GoalDocumentError) as exc:
            notes.append(self._quarantine_goals(store, exc))
        if snapshot is not None and snapshot.records:
            now = self.clock()
            try:
                with self.goal_lock:
                    lost = queue.restore(snapshot, now_ms=now)
            except ValueError as exc:
                # Parsed, but not a channel any queue could have written (an id
                # twice, an over-full backlog). Same evidence rule as above;
                # restore left the queue untouched, so empty is what starts.
                notes.append(self._quarantine_goals(store, exc))
            else:
                pending = sum(1 for r in snapshot.records if r.state is GoalState.PENDING)
                notes.append(
                    f"restored {pending} pending goal(s) and "
                    f"{len(snapshot.records) - pending - len(lost)} finished record(s) from "
                    f"{store.path.name}; {len(lost)} goal(s) active at the previous "
                    "shutdown are recorded failed (session_terminated)"
                )
        # Persist what adoption itself established — including the restored
        # ACTIVE goal's terminal record, so a second restart before the first
        # tick still answers for it. A first run with no file writes nothing:
        # the flush below sees revision unchanged and the file appears with the
        # first real transition.
        if snapshot is not None:
            self._flush_goals(force=True)
        else:
            with self.goal_lock:
                self._goal_flushed_revision = queue.revision
        return tuple(notes)

    def _quarantine_goals(self, store: GoalStore, cause: Exception) -> str:
        """Set the unreadable goals file aside; say what happened either way."""
        try:
            aside = store.quarantine()
        except GoalStoreError as rename_failure:
            note = (
                f"the stored goals could not be read ({cause}) and the file could not be "
                f"set aside either ({rename_failure}); the goal channel starts empty"
            )
        else:
            note = (
                f"the stored goals could not be read ({cause}); the file was set aside as "
                f"{aside.name} and the goal channel starts empty"
            )
        self._note_state_problem(note)
        return note

    def _flush_goals(self, *, force: bool = False) -> None:
        """Write the goal channel to its store when a transition made it stale.

        The cadence, stated once: at most one write per tick, and none on a
        tick in which no goal changed — the queue's ``revision`` counts record
        mutations, and an unchanged revision is skipped without even building
        the snapshot. Transitions made between ticks by the serving thread
        (a submit over the port) are caught by the next tick's flush, and
        :meth:`shutdown` flushes once more after its disarm, so the last state
        a client can be told is also the last state on disk. The snapshot is
        taken under ``goal_lock``; the write happens after it is released,
        keeping that lock's no-IO promise.

        E12-M03-T004 for goals: a write that fails is reported on
        :attr:`state_problems` and retried on the next transition (or next
        tick, the revision being still unflushed) rather than ending a session
        that is driving a character over its own bookkeeping.
        """
        store = self.goal_store
        queue = self.goals
        if store is None or queue is None:
            return
        with self.goal_lock:
            if not force and queue.revision == self._goal_flushed_revision:
                return
            snapshot = queue.snapshot()
        try:
            store.save_snapshot(snapshot)
        except GoalStoreError as exc:
            self._note_state_problem(
                f"the goal channel could not be persisted ({exc}); a restart before the "
                "next successful write will not remember these goals"
            )
            return
        self._goal_flushed_revision = snapshot.revision

    def _panic_actions(self) -> None:
        """End every waiting remote submission as the panic stop's own casualty.

        Called before the disarm on every panic tick — a level, not an edge,
        exactly as :meth:`_panic_goals` is — so the record carries
        ``PANIC_STOP`` rather than the disarm's ``NOT_ARMED``: the submitter is
        told which lever was pulled, not merely that authority is gone.
        """
        channel = self.actions
        if channel is None:
            return
        channel.clear_pending(
            ReasonCode.PANIC_STOP,
            status=ActionStatus.CANCELLED,
            message="a panic stop cleared the remote action channel",
        )

    def _confirm(self, action: ActionName, result: ActionResult) -> Capability | None:
        """Offer one outcome to the ledger as evidence about a capability.

        The static check said the symbols are on disk, which is a reason to try;
        this is the other half — a live run is the only thing in the project that
        may raise a capability to ``verified``, and without this call
        :func:`~pz_agent_core.capabilities.probes.confirm` is never reached
        outside a test and nothing ever leaves ``available_unverified``.

        Nothing is offered for a result that is not a success: the engine only
        constructs one after an adapter observed the postcondition, so an
        earlier exit is a claim about a world nobody looked at.
        """
        ledger = self.capabilities
        if ledger is None or result.status is not ActionStatus.SUCCEEDED:
            return None
        capability = self.registry.get(action).required_capability
        if capability is None:
            return None
        return ledger.confirm_capability(capability, action, result)

    # -- the trace ``pz-agent replay`` reads -------------------------------

    def _note_dispatch(self, command: Command) -> None:
        """Hold a shipped command until its terminal result arrives.

        The engine returns a result, not a command, so this is the only place
        the arguments the adapter actually built are visible from here. The ring
        is tiny because the pairing window is one action: the engine drives each
        command to a terminal result before it returns, and the cap exists so a
        result that never arrives cannot grow this without bound.
        """
        if self.trace is None:
            return
        self._shipped[command.command_id] = command
        while len(self._shipped) > MAX_TRACED_COMMANDS:
            self._shipped.popitem(last=False)

    def _trace_results(self, results: Sequence[ActionResult]) -> None:
        """Record each terminal result against the command that produced it."""
        trace = self.trace
        if trace is None:
            return
        for result in results:
            command = self._shipped.pop(result.command_id, None)
            try:
                if command is None:
                    trace.record_refusal(result)
                else:
                    trace.record_action(command, result)
            except (OSError, DiagnosticsError, TraceError):
                # Losing an entry is not a reason to abandon a tick that is
                # driving a character. The next write attempts again.
                return

    def _trace_observation(self) -> None:
        """Record the world as this tick found it, if it moved.

        Which of a snapshot and a diff gets written is the trace format's
        decision rather than this loop's — :meth:`TraceWriter.record_world`
        owns it, next to the reader whose refusals it exists to avoid. What the
        loop owns is the baseline and the one case worth skipping entirely: a
        tick that ingested nothing has no news, and an empty diff per tick would
        fill the file with the fact that nothing happened.
        """
        trace = self.trace
        current = self._store.latest()
        if trace is None or current is None:
            return
        baseline = self._traced
        unchanged = (
            baseline is not None
            and baseline.session_id == current.session_id
            and baseline.seq == current.seq
        )
        if unchanged:
            return
        try:
            trace.record_world(current, baseline)
        except (OSError, DiagnosticsError, DiffError, TraceError):
            return
        self._traced = current

    def _publish_heartbeat(self) -> None:
        attached = self._attached
        manager = self._session_manager()
        nonce = manager.sidecar_nonce
        if attached is None or nonce is None:
            return
        self._heartbeats().publish(
            Peer.SIDECAR,
            session_id=attached.session.session_id,
            nonce=nonce,
            version=PRODUCT_VERSION,
            armed=self._armed,
            mode=self._mode,
        )

    # -- the whole loop ----------------------------------------------------

    def run(self, *, should_stop: Callable[[], bool] | None = None) -> RunSummary:
        """Tick until something says to stop, or until the budget is spent.

        The budget is the outer bound and it is a ``range``, not a condition: a
        clock that stops moving, a stop file that never lands and a game that
        never comes back all end here rather than running forever.
        """
        self._require_attached()
        last: TickOutcome | None = None
        for index in range(self.limits.tick_budget):
            last = self.tick()
            if last.stop is not None:
                return RunSummary(
                    ticks=index + 1,
                    cause=last.stop,
                    detail=f"stopped on tick {index + 1}: {last.stop.value}",
                    last=last,
                )
            if should_stop is not None and should_stop():
                return RunSummary(
                    ticks=index + 1,
                    cause=StopCause.CALLER_ASKED,
                    detail=f"the caller asked to stop on tick {index + 1}",
                    last=last,
                )
            if index + 1 < self.limits.tick_budget:
                # No pause before returning: the budget being spent is not a
                # reason to wait, and a test that drives one tick should not
                # have to wait out an interval it will never use.
                self.sleep(self.limits.tick_interval_ms)
        return RunSummary(
            ticks=self.limits.tick_budget,
            cause=StopCause.TICK_BUDGET,
            detail=f"the tick budget of {self.limits.tick_budget} was spent",
            last=last,
        )
