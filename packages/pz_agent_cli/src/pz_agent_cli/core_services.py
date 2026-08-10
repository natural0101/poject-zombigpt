"""The real sidecar behind the Core RPC router: `CoreServices` over `SidecarLoop`.

Until this module existed, :class:`~pz_agent_mcp.remote.server.CoreRouter` was
constructed only by tests, over fakes. This is the missing half of R-008: the
port bundle the router reads through, implemented over the subsystems the
running loop actually holds — its observation store, its mode and armed state,
its capability ledger, its memory, its diagnostic log — and one function,
:func:`serve_core_rpc`, that ``pz-agent start`` calls to put the router behind
:meth:`~pz_agent_cli.supervisor.SidecarSupervisor.serve_rpc`. The descriptor
and token lifecycle stays :class:`~pz_agent_cli.supervisor.SidecarRpc`'s;
nothing here duplicates it.

The thread seam, designed rather than assumed
---------------------------------------------

:class:`~pz_agent_core.rpc.transport.RpcServer` answers each request on its own
serving thread while :meth:`~pz_agent_cli.runtime.SidecarLoop.run` ticks on the
main thread. Every port below states which side of that seam it stands on:

* **Reads are single references to immutable state, taken once per call.**
  ``loop.mode`` and ``loop.armed`` are plain attributes replaced whole;
  ``loop.store.latest()`` reads the tail of a ``deque`` whose elements are
  frozen :class:`~pz_agent_core.protocol.Observation` records;
  ``loop.capabilities.report`` and ``memory.loaded`` are references the main
  thread only ever replaces with a fully built successor. Under the GIL each
  such read observes the previous value or the new one, never a torn one.
  Heartbeat liveness and the log tail are file reads with no shared state at
  all. Nothing here iterates a container the tick thread mutates.

* **The goal channel crosses on one lock, held short, with no IO under it.**
  :class:`~pz_agent_core.goals.GoalQueue` is not thread-safe, so its seam is
  designed rather than assumed: every touch of the queue — this serving
  thread's ``goal.submit`` / ``goal.status`` / ``goal.cancel`` and the tick
  thread's activation, settlement, expiry tick, disarm and panic clear — takes
  :attr:`~pz_agent_cli.runtime.SidecarLoop.goal_lock` and nothing else. Every
  hold is bounded by construction: the queue's operations are pure in-memory
  transitions over at most ``max_remembered`` records, and the tick thread
  releases the lock before it consults the planner or drives the engine, so no
  planner call, no engine call, no file and no sleep ever happens under it. No
  cross-thread mutation of the queue exists outside this seam, and every
  answer :class:`LoopGoalPort` returns is the queue's own — the real admission
  with its duplicate detection, the real channel state, the real cancellation
  — never fabricated here.

* **Writes go through the shipped control channel, never into loop state.**
  ``session.arm`` and ``session.disarm`` publish the same one-slot request file
  ``pz-agent arm``/``disarm`` write, and then wait — bounded by
  :data:`DEFAULT_CONTROL_WAIT_S` and a poll count, never an unbounded spin —
  for the loop to publish its :class:`~pz_agent_cli.runtime.ControlDecision`
  for that request's nonce. Success is the loop's own observed outcome;
  refusal is relayed in the loop's own words; a loop that never answers is a
  named refusal, not a fabricated success. ``session.stop`` writes the panic
  sentinel the mod and the loop both read as a level — the same latch the
  voice companion's stop uses — and reports only what it observed.

* **The wait happens on the serving thread, and that is acceptable because the
  stop levers do not travel this link's queue.** The transport serves one
  request at a time, so a waiting ``arm`` delays the next RPC by at most
  :data:`DEFAULT_CONTROL_WAIT_S` — well inside the client's deadline — while
  the panic sentinel and the control-file stop are plain file writes that any
  process performs without this server's help.

What is served, what refuses, and why the refusals are honest
-------------------------------------------------------------

``session``, ``observations``, ``capabilities``, ``memory``, ``diagnostics``,
``goals`` (over a loop that holds a :class:`~pz_agent_core.goals.GoalQueue`
beside a goal-capable planner) and ``actions`` are served over the real
subsystems. One surface is not, and it refuses with a named reason rather than
stubbing:

* **Plans** (:data:`REMOTE_PLANS_UNSERVED`): ``plan.execute`` would drive many
  engine calls to their terminal results inside one call, and the machinery
  the loop actually has cannot do that honestly across this seam — see
  :class:`UnservedPlanPort` for what was found. ``plan.current`` answers
  ``None`` because no typed plan is ever running here, which is a fact and not
  a placeholder.

**Actions** moved off that list the way goals did, by building what the old
refusal named as missing: a bounded cross-thread submission path. The loop owns
an :class:`~pz_agent_cli.runtime.ActionChannel` — a capped queue whose
admission refuses when full, plus a bounded record store of frozen
:class:`~pz_agent_mcp.ports.ActionRecord` instances replaced whole on every
transition — and drains at most one submission per tick through the *same*
funnel a planner proposal takes (arming gate, action budget, the engine's own
capability and permission machinery), so every dispatch still happens on the
tick thread. :class:`LoopActionPort` is the serving-thread half: ``submit``
admits and returns the queued record without waiting, ``status`` answers the
channel's own record or ``None`` for an id it never minted. A disarm, a panic
sentinel and a shutdown each end what is still waiting with a terminal record
naming the lever (``NOT_ARMED``, ``PANIC_STOP``, ``CANCELLED_BY_REQUEST``).
For a loop assembled *without* a channel the bundle keeps
:class:`UnservedActionPort`'s named refusal (:data:`REMOTE_ACTIONS_UNSERVED`)
— a port that admitted submissions nothing would ever drain is exactly the
accepted-and-dropped port :mod:`pz_agent_mcp.ports` names as forbidden.

The goal channel was the first surface to move off that list: the loop holds a
:class:`~pz_agent_core.goals.GoalQueue` and, armed in ``AUTONOMOUS`` with a
:class:`~pz_agent_cli.runtime.GoalPlanner`, activates admitted goals and hands
each to its planner (see :meth:`~pz_agent_cli.runtime.SidecarLoop._act`). For a
loop assembled *without* both halves the bundle's ``goals`` still stays
``None`` — a channel that admitted goals it can never serve is exactly the
accepted-and-dropped port :mod:`pz_agent_mcp.ports` names as forbidden — and
the router's own :data:`~pz_agent_mcp.remote.server.NO_GOAL_CHANNEL` answers
the three goal methods with its named refusal.

``confirm_backup=False`` on ``session.arm`` is refused here
(:data:`ARM_NEEDS_BACKUP`) because this wiring *is* the core side of that
boundary: the port's contract says arming without a backed-up save is the
core's refusal to make, and the loop beneath has no other place to make it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.diagnostics import read_records
from pz_agent_core.goals import GoalAdmission, GoalQueue, GoalRequest
from pz_agent_core.ipc.atomic import guard_managed
from pz_agent_core.memory.store import SaveMemory
from pz_agent_core.protocol import (
    DangerLevel,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.session.heartbeat import Peer
from pz_agent_core.version import PROTOCOL_VERSION
from pz_agent_mcp.ports import (
    ActionRecord,
    CoreServices,
    DoctorCheck,
    GoalCancellation,
    GoalChannelStatus,
    LogRecord,
    MemoryRecord,
    PlanRecord,
    PlanRequest,
    SessionSnapshot,
    StopReport,
)
from pz_agent_mcp.remote.server import NO_GOAL_CHANNEL, CoreRouter

from .autonomy import AutonomyPlanner
from .doctor import DoctorReport
from .memory import SidecarMemory
from .navigation_planner import unwrap_planner
from .runtime import ActionChannel, ControlDecision, GoalPlanner, LoopError, SidecarLoop
from .supervisor import ControlChannel, ControlKind, RpcEndpoint, SidecarSupervisor

__all__ = [
    "ARM_NEEDS_BACKUP",
    "CONTROL_UNANSWERED",
    "DEFAULT_CONTROL_WAIT_S",
    "MEMORY_KINDS",
    "REMOTE_ACTIONS_UNSERVED",
    "REMOTE_PLANS_UNSERVED",
    "LoopActionPort",
    "LoopCapabilityPort",
    "LoopDiagnosticsPort",
    "LoopGoalPort",
    "LoopMemoryPort",
    "LoopObservationPort",
    "LoopSessionPort",
    "UnservedActionPort",
    "UnservedPlanPort",
    "core_services_over",
    "serve_core_rpc",
]

#: How long a control write waits for the loop to judge it. The budget has to
#: cover the two-phase arm from the far side: the loop publishes an arm's
#: :class:`~pz_agent_cli.runtime.ControlDecision` only once the game's own
#: confirmation — or its refusal, or the
#: :data:`~pz_agent_cli.runtime.DEFAULT_ARM_CONFIRM_TIMEOUT_MS` deadline (5 s)
#: — has been observed, so eight seconds is that whole window plus ticks of
#: slack while staying inside the transport client's ten-second deadline. A
#: slow confirmation reads as a slow answer rather than a dead link.
DEFAULT_CONTROL_WAIT_S: Final = 8.0

#: How often the waiter looks for the loop's decision. The poll count derived
#: from it is the second bound: a monotonic clock that stopped moving must not
#: turn the deadline into a spin.
CONTROL_POLL_S: Final = 0.02

#: The named refusal ``action.submit`` answers over a loop assembled without an
#: :class:`~pz_agent_cli.runtime.ActionChannel`. A refusal is honest; a queue
#: that accepted work nothing drains would not be.
REMOTE_ACTIONS_UNSERVED: Final = (
    "this sidecar was assembled without a remote action channel, so nothing would ever "
    "drain a submission it accepted; restart the sidecar with its default assembly, or "
    "submit a typed goal — this build serves the goal channel"
)

#: The named refusal ``plan.execute`` answers; :class:`UnservedPlanPort` says why.
REMOTE_PLANS_UNSERVED: Final = (
    "this sidecar does not execute remote plans: a plan is many engine calls driven to "
    "their terminal results in one request, and the loop's tick thread cannot hold "
    "them without putting its own stop levers out of reach. Submit a typed goal — the "
    "served channel that drives multi-step work one bounded step per tick"
)

#: The named refusal an ``arm`` without a backup confirmation gets. Refusing is
#: the safe direction: arming on a promise nobody made is how a save is lost.
ARM_NEEDS_BACKUP: Final = (
    "arming was requested without confirm_backup: make a backup ('pz-agent backup-save') "
    "and pass confirm_backup=true — the agent will not take authority over a save nobody "
    "has protected"
)

#: The named refusal a control request that nobody consumed gets.
CONTROL_UNANSWERED: Final = (
    "the running loop did not act on the request in time; it may be stalled, mid-action "
    "or shutting down — check 'pz-agent status' and ask again"
)

#: The record kinds :class:`LoopMemoryPort` serves, in the order they are
#: emitted. A closed set, so a caller can filter by names that exist.
MEMORY_KINDS: Final[tuple[str, ...]] = (
    "home",
    "reservation",
    "safe_zone",
    "container",
    "preference",
    "failed_path",
)

#: Component reported for every record of the sidecar's own log, derived below
#: from the event's leading segment when it has one.
_LOG_COMPONENT: Final = "sidecar"


@dataclass
class _ControlWaiter:
    """Writes one control request and waits, bounded, for the loop's decision.

    ``sleep`` and ``monotonic`` are injected so a unit test drives the loop's
    ticks from inside the wait instead of relying on a second thread. The wait
    is doubly bounded — a deadline on the injected clock and a poll count — for
    the same reason :class:`~pz_agent_cli.runtime.JournalObservationSource`
    bounds its poll loop.
    """

    loop: SidecarLoop
    wait_s: float = DEFAULT_CONTROL_WAIT_S
    poll_s: float = CONTROL_POLL_S
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if self.wait_s <= 0 or self.poll_s <= 0:
            raise LoopError("the control waiter's bounds must be positive")

    def _channel(self) -> ControlChannel:
        channel = self.loop.control
        if channel is None:
            raise LoopError("the loop has no control channel; it was never assembled")
        return channel

    def request(self, kind: ControlKind, *, mode: SessionMode | None = None) -> ControlDecision:
        """Publish a request and return the loop's own judgement of it.

        Raises:
            LoopError: the loop never consumed the request within the bound
                (:data:`CONTROL_UNANSWERED`), or the request itself was
                malformed (the channel's own refusal, e.g. arming into
                ``OBSERVE``).
        """
        request = self._channel().write(kind, mode=mode)
        deadline = self.monotonic() + self.wait_s
        polls = max(1, int(self.wait_s / self.poll_s) + 1)
        for _ in range(polls):
            decision = self.loop.control_decision
            if decision is not None and decision.nonce == request.nonce:
                return decision
            if self.monotonic() >= deadline:
                break
            self.sleep(self.poll_s)
        raise LoopError(f"{kind.value}: {CONTROL_UNANSWERED}")

    def until(self, done: Callable[[], bool]) -> bool:
        """Wait, bounded the same way, until *done* answers True. Returns what it saw."""
        deadline = self.monotonic() + self.wait_s
        polls = max(1, int(self.wait_s / self.poll_s) + 1)
        for _ in range(polls):
            if done():
                return True
            if self.monotonic() >= deadline:
                break
            self.sleep(self.poll_s)
        return done()


@dataclass(frozen=True)
class LoopSessionPort:
    """:class:`~pz_agent_mcp.ports.SessionPort` over the running loop.

    Reads take single references off the loop and file reads off the exchange
    directory; the two levers that change state travel the shipped control
    channel and the panic sentinel. See the module docstring for the seam.
    """

    loop: SidecarLoop
    waiter: _ControlWaiter

    def status(self) -> SessionSnapshot:
        """The session as the loop holds it right now.

        Raises:
            LoopError: the loop is not attached — after shutdown, or before
                attach. A snapshot invented for a loop with no session would
                name a session that does not exist.
        """
        loop = self.loop
        session = loop.session
        if session is None:
            raise LoopError("the sidecar loop is not attached to an exchange directory")
        monitor = loop.monitor
        assert monitor is not None  # set in SidecarLoop.__post_init__
        game = monitor.liveness(Peer.GAME)
        sidecar = monitor.liveness(Peer.SIDECAR)
        latest = loop.store.latest()
        ledger = loop.capabilities
        report = None if ledger is None else ledger.report
        queue = loop.queue
        in_flight = None if queue is None else queue.in_flight
        return SessionSnapshot(
            session_id=session.session_id,
            mode=loop.mode,
            armed=loop.armed,
            connected=game.alive,
            protocol_version=PROTOCOL_VERSION,
            build=None if game.heartbeat is None else game.heartbeat.build,
            save_id=session.save_id,
            danger_level=DangerLevel.NONE if latest is None else latest.safety.danger_level,
            observation_seq=None if latest is None else latest.seq,
            capability_revision=0 if report is None else report.revision,
            game_heartbeat_ok=game.alive,
            sidecar_heartbeat_ok=sidecar.alive,
            active_action_id=None if in_flight is None else in_flight.command_id,
        )

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        """Ask the loop to arm, through the shipped control channel, and wait.

        The wait spans the whole two-phase arm: the loop submits ``session.arm``
        to the mod and publishes this request's decision only once the game's
        terminal ack *and* a heartbeat reporting the armed mode were observed —
        or once the refusal was (:meth:`~pz_agent_cli.runtime.SidecarLoop.arm`).
        A decision that says armed is therefore the game's own word, never the
        sidecar's opinion of itself; that distinction is the 2026-08-08 live
        finding this port exists downstream of.

        Raises:
            LoopError: no backup was confirmed (:data:`ARM_NEEDS_BACKUP`), the
                loop refused — its own detail is relayed verbatim — or the loop
                never consumed the request (:data:`CONTROL_UNANSWERED`).
        """
        if not confirm_backup:
            raise LoopError(ARM_NEEDS_BACKUP)
        decision = self.waiter.request(ControlKind.ARM, mode=mode)
        if not decision.armed or decision.mode is not mode:
            raise LoopError(decision.detail)
        return self.status()

    def disarm(self) -> SessionSnapshot:
        """Return the loop to ``OBSERVE`` through the same channel. Never gated."""
        self.waiter.request(ControlKind.DISARM)
        return self.status()

    def stop(self) -> StopReport:
        """Latch the panic stop and report what was observed, nothing more.

        The sentinel is the same file the mod and the loop both read as a
        level, written the way the voice companion writes it: whole, non-empty,
        verified by its own size. The loop observes it on its next tick,
        disarms and closes in-flight work as lost; the wait below is a bounded
        courtesy so the common answer carries ``disarmed=True`` as an observed
        fact — a loop that has not ticked yet is honestly reported still armed.

        ``cleared`` stays 0 because the count of mod-owned queue entries
        belongs to the mod, which is where it is recorded; a number invented
        here would be a number nobody measured.
        """
        layout = self.loop.layout
        latch = guard_managed(layout, layout.panic_stop)
        latch.write_text(
            f'{{"requested_at_ms":{self.loop.clock()},"source":"core-rpc",'
            f'"reason":"{ReasonCode.PANIC_STOP.value}"}}\n',
            encoding="utf-8",
        )
        if latch.stat().st_size == 0:
            raise LoopError("the panic latch was written empty, so it stops nothing")
        self.waiter.until(lambda: not self.loop.armed)
        return StopReport(
            cleared=0,
            disarmed=not self.loop.armed,
            mode=self.loop.mode,
        )


@dataclass(frozen=True)
class LoopObservationPort:
    """:class:`~pz_agent_mcp.ports.ObservationPort` over the loop's real store.

    One reference read off a deque of frozen records; safe cross-thread, and
    ``None`` means exactly what the port says it means — nothing has arrived.
    """

    loop: SidecarLoop

    def latest(self) -> Observation | None:
        return self.loop.store.latest()


@dataclass(frozen=True)
class LoopCapabilityPort:
    """:class:`~pz_agent_mcp.ports.CapabilityPort` over the loop's real ledger.

    The ledger's ``report`` reference is only ever replaced whole by the tick
    thread (a confirmation builds the successor first), so the read is safe.
    """

    loop: SidecarLoop

    def report(self) -> CapabilityReport:
        """The scan-backed report, or the ledger's own refusal.

        Raises:
            LoopError: no ledger was assembled, or the scan could not run. The
                detail is the ledger's own; an empty report invented here would
                read as a game that supports nothing.
        """
        ledger = self.loop.capabilities
        if ledger is None:
            raise LoopError("this sidecar was assembled without a capability ledger")
        report = ledger.report
        if report is None:
            raise LoopError(f"capabilities were not resolved: {ledger.detail}")
        return report


@dataclass(frozen=True)
class UnservedActionPort:
    """:class:`~pz_agent_mcp.ports.ActionPort` that refuses, by name.

    The port a loop assembled *without* an
    :class:`~pz_agent_cli.runtime.ActionChannel` gets. Nothing would drain a
    submission such a loop accepted, and a channel that admits what it cannot
    serve is the forbidden accepted-and-dropped port; the default assembly
    (``SidecarLoop.__post_init__``) always builds a channel, so this refusal
    is reserved for wiring that deliberately removed it.
    """

    def submit(self, request: ActionRequest) -> ActionRecord:
        raise LoopError(REMOTE_ACTIONS_UNSERVED)

    def status(self, action_id: str) -> ActionRecord | None:
        """``None``, truthfully: this core has never minted a remote action id."""
        return None


@dataclass(frozen=True)
class LoopActionPort:
    """:class:`~pz_agent_mcp.ports.ActionPort` over the loop's own action channel.

    The serving-thread half of the seam :class:`~pz_agent_cli.runtime.ActionChannel`
    documents. :meth:`submit` admits and returns as soon as the submission has
    an id — ``accepted``, the honest word for queued, exactly as the port's
    contract requires so a long action can never hold the transport the stop
    tool needs. :meth:`status` answers the channel's real record — the queued
    claim, the started claim, or the terminal record carrying the engine's own
    result — and ``None`` only for an id this process cannot answer for: never
    minted here, evicted after its record turned terminal, or minted by a
    previous run of the sidecar (the restart case; the channel's store dies
    with its process). A live record always answers, so the boundary above can
    honestly tell "unknown id" apart from every lifecycle state — it must
    relay ``None`` as "unknown to this sidecar", never guess a status for it.
    Every answer is the channel's; nothing is decided on this side of its
    lock.
    """

    loop: SidecarLoop

    def _channel(self) -> ActionChannel:
        channel = self.loop.actions
        if channel is None:
            raise LoopError(REMOTE_ACTIONS_UNSERVED)
        return channel

    def submit(self, request: ActionRequest) -> ActionRecord:
        """Admit *request* to the loop's queue, or relay the refusal. Never waits.

        Raises:
            LoopError: the loop is not attached — nothing would ever drain the
                queue, so admitting to it would be the accepted-and-dropped
                port — or the channel's own refusal: a full queue, or an
                idempotency key reused for different work.
        """
        channel = self._channel()
        if self.loop.session is None:
            raise LoopError(
                "the sidecar loop is not attached to an exchange directory, so a "
                "submission would wait on a tick that never comes"
            )
        return channel.submit(request)

    def status(self, action_id: str) -> ActionRecord | None:
        return self._channel().status(action_id)


@dataclass(frozen=True)
class UnservedPlanPort:
    """:class:`~pz_agent_mcp.ports.PlanPort`: execution refuses, ``current`` is honest.

    Kept a refusal on purpose after the action channel was built, because what
    exists cannot honestly drive a remote plan, and the reasons are specific:

    * The local plan machinery, :class:`~pz_agent_core.planner.PlanExecutor`,
      runs a whole plan — replans, per-step retries and all — inside one call,
      bounded only by its own budgets. On the tick thread that call would hold
      the loop for the plan's whole wall budget, and the loop's stop levers
      (the control channel's disarm, the panic sentinel's queue clear) are
      consumed *between* ticks: a running plan would put them out of reach for
      minutes, which is exactly the property the action channel was shaped to
      avoid by dispatching one bounded action per tick.
    * :class:`~pz_agent_mcp.ports.PlanRequest` carries a free-text ``goal``.
      The only thing in this project that turns free text into a typed plan is
      an LLM provider, and the deterministic loop must not be the place where
      untrusted text is promoted into intent (AGENTS.md: the model may express
      a goal; it never picks the sandwich).

    The served shape for multi-step remote work already exists and is the goal
    channel: a typed :class:`~pz_agent_core.goals.GoalRequest` from a closed
    kind set, activated by the loop and driven one budgeted step per tick with
    every stop lever applied between steps. A ``plan.execute`` that did less
    would be a half-served plan; this refusal is the honest answer until a
    step-per-tick plan runner exists on the loop's side of the seam.
    """

    def execute(self, request: PlanRequest) -> PlanRecord:
        raise LoopError(REMOTE_PLANS_UNSERVED)

    def current(self) -> PlanRecord | None:
        """``None``: no typed plan ever runs in this sidecar, so none is running."""
        return None


@dataclass(frozen=True)
class LoopGoalPort:
    """:class:`~pz_agent_mcp.ports.GoalPort` over the loop's own goal queue.

    Every answer is the queue's: :meth:`submit` returns the real admission —
    including the duplicate a resubmitted idempotency key resolves to and the
    refusal a reused-with-different-content key earns — :meth:`status` the real
    channel state, :meth:`cancel` the real cancellation, which is a *request*
    the loop's next tick applies, exactly as
    :class:`~pz_agent_mcp.ports.GoalCancellation` documents. Nothing is
    fabricated on this side of the lock.

    The seam (see the module docstring): the queue is only ever touched under
    :attr:`~pz_agent_cli.runtime.SidecarLoop.goal_lock`, shared with the tick
    thread; every hold below is a handful of in-memory dictionary operations,
    no IO, no waiting.
    """

    loop: SidecarLoop

    def _queue(self) -> GoalQueue:
        queue = self.loop.goals
        if queue is None:
            raise LoopError("this sidecar was assembled without a goal queue")
        return queue

    def submit(self, request: GoalRequest) -> GoalAdmission:
        """Admit *request*, or relay the queue's own refusal. Never waits."""
        queue = self._queue()
        with self.loop.goal_lock:
            return queue.submit(request)

    def status(self, goal_id: str | None = None) -> GoalChannelStatus:
        """The channel as the queue holds it right now, one consistent read.

        ``named`` is ``None`` both for "no id asked about" and for an id this
        channel never minted or has forgotten; the router knows which it
        passed and turns the second into its own refusal.
        """
        queue = self._queue()
        with self.loop.goal_lock:
            named = queue.record(goal_id) if goal_id is not None else None
            return GoalChannelStatus(active=queue.active, pending=queue.pending, named=named)

    def cancel(self, goal_id: str) -> GoalCancellation:
        """Ask the queue to end *goal_id*; report what the ask did, nothing more."""
        queue = self._queue()
        with self.loop.goal_lock:
            goal = queue.record(goal_id)
            if goal is None:
                return GoalCancellation(goal=None, requested=False)
            requested = queue.request_cancel(goal_id)
            return GoalCancellation(goal=queue.record(goal_id) or goal, requested=requested)


@dataclass(frozen=True)
class LoopMemoryPort:
    """:class:`~pz_agent_mcp.ports.MemoryPort` over the loop's real save memory.

    ``memory.loaded`` is a reference the tick thread replaces with a fully
    built :class:`~pz_agent_core.memory.store.SaveMemory` and never mutates
    in-process afterwards (only ``pz-agent remember`` in another process writes
    the file, and that arrives as a fresh load), so reading it cross-thread is
    a copy-on-read of immutable state.

    Task history is deliberately not served: its ``outcome`` is an enum the
    record's numeric ``data`` cannot carry, and folding it into the quarantined
    ``label`` would hide a machine-readable fact in human text. The boundary is
    recorded here rather than papered over.
    """

    memory: SidecarMemory | None

    def query(self, *, kinds: Sequence[str], limit: int) -> Sequence[MemoryRecord]:
        """Remembered facts in force right now, filtered and bounded.

        Raises:
            LoopError: no memory was wired, or this save's memory exists and
                cannot be read — answering "nothing remembered" over a file
                holding the user's reservations would be the lie
                :class:`~pz_agent_cli.memory.UnreadableMemory` exists to
                prevent, so the memory's own detail is relayed instead.
        """
        memory = self.memory
        if memory is None:
            raise LoopError("this sidecar was assembled without a save memory")
        if not memory.record().readable:
            raise LoopError(memory.detail)
        loaded = memory.loaded
        if loaded is None:
            return ()
        wanted = frozenset(kinds)
        records = [
            record for record in _memory_records(loaded) if not wanted or record.kind in wanted
        ]
        return tuple(records[: max(0, limit)])


def _memory_records(loaded: SaveMemory) -> Iterator[MemoryRecord]:
    """*loaded* as boundary records, kinds in :data:`MEMORY_KINDS` order.

    Numbers and booleans go in ``data``, the one human string in ``label``,
    which is the shape that leaves a store nowhere to smuggle unquarantined
    text; see :class:`~pz_agent_mcp.ports.MemoryRecord`.
    """
    home = loaded.home_point()
    if home is not None:
        yield MemoryRecord(
            kind="home",
            key="home",
            data={"x": home.x, "y": home.y, "z": home.z, "set_at_ms": loaded.home_set_at_ms},
        )
    for reservation in loaded.reservations():
        yield MemoryRecord(
            kind="reservation",
            key=reservation.full_type,
            label=reservation.reason or None,
            data={"created_at_ms": reservation.created_at_ms},
        )
    for zone in loaded.safe_zones():
        yield MemoryRecord(
            kind="safe_zone",
            key=zone.key,
            label=zone.label or None,
            data={
                "x": zone.centre.x,
                "y": zone.centre.y,
                "z": zone.centre.z,
                "radius": zone.radius,
                "recorded_at_ms": zone.recorded_at_ms,
            },
        )
    for container in loaded.containers():
        square = container.square
        data: dict[str, float | int | bool | None] = {
            "last_seen_ms": container.last_seen_ms,
            "last_inspected_ms": container.last_inspected_ms,
        }
        if square is not None:
            data.update({"x": square.x, "y": square.y, "z": square.z})
        yield MemoryRecord(
            kind="container",
            key=container.tail,
            label=container.label or None,
            data=data,
        )
    for preference in loaded.preferences():
        yield MemoryRecord(
            kind="preference",
            key=preference.key,
            label=preference.value or None,
            data={"set_at_ms": preference.set_at_ms},
        )
    for path in loaded.failed_paths():
        yield MemoryRecord(
            kind="failed_path",
            key=path.key,
            label=path.reason or None,
            data={
                "origin_x": path.origin.x,
                "origin_y": path.origin.y,
                "origin_z": path.origin.z,
                "target_x": path.target.x,
                "target_y": path.target.y,
                "target_z": path.target.z,
                "attempts": path.attempts,
                "last_failed_ms": path.last_failed_ms,
            },
        )


@dataclass(frozen=True)
class LoopDiagnosticsPort:
    """:class:`~pz_agent_mcp.ports.DiagnosticsPort` over the shipped diagnostics.

    ``doctor`` is the same :func:`~pz_agent_cli.doctor.run_checks` the CLI
    runs, injected as a callable because it needs the workspace this module
    deliberately does not hold; its checks arrive already redacted. The tail
    reads the loop's own JSONL log file — a file read with no shared state, so
    the seam costs nothing. Both run on the serving thread and both are
    bounded: the doctor by its own scan bounds, the tail by ``limit`` and the
    reader's byte cap.
    """

    doctor_source: Callable[[], DoctorReport]
    log_file: Path | None

    def doctor(self) -> Sequence[DoctorCheck]:
        report = self.doctor_source()
        return tuple(
            DoctorCheck(
                code=check.code,
                ok=not check.failed,
                detail=check.detail,
                remediation=check.remediation,
            )
            for check in report.checks
        )

    def tail(
        self,
        *,
        limit: int,
        level: str | None = None,
        component: str | None = None,
        action_id: str | None = None,
    ) -> Sequence[LogRecord]:
        """The newest matching records of the sidecar's structured log.

        A missing file answers an empty sequence — "no log yet" is what a
        fresh state directory truthfully holds. A sidecar assembled without a
        log directory refuses instead, because for it the absence is permanent.

        Raises:
            LoopError: this sidecar has nowhere it writes a log.
        """
        if self.log_file is None:
            raise LoopError(
                "this sidecar has no diagnostic log to tail; its log directory could "
                "not be created at startup"
            )
        if limit < 1:
            return ()
        raw, _problems = read_records(self.log_file, limit=limit)
        records = [
            record
            for payload in raw
            if (record := _log_record(payload)) is not None
            and _matches(record, level=level, component=component, action_id=action_id)
        ]
        return tuple(records[-limit:])


def _log_record(payload: dict[str, object]) -> LogRecord | None:
    """One structured log line as the boundary's record, or None if unusable.

    A line that lost a required field is skipped rather than invented:
    :func:`~pz_agent_core.diagnostics.read_records` already reports unreadable
    lines as problems, and a half-record here would attribute words to a log
    that never wrote them.
    """
    timestamp = payload.get("timestamp_ms")
    level = payload.get("level")
    event = payload.get("event")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        return None
    if not isinstance(level, str) or not isinstance(event, str) or not event:
        return None
    fields = payload.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    code = fields.get("code")
    action_id = fields.get("action_id")
    component = event.split(".", 1)[0] if "." in event else _LOG_COMPONENT
    return LogRecord(
        timestamp_ms=timestamp,
        level=level,
        component=component,
        message=event,
        code=code if isinstance(code, str) else None,
        action_id=action_id if isinstance(action_id, str) else None,
    )


def _matches(
    record: LogRecord,
    *,
    level: str | None,
    component: str | None,
    action_id: str | None,
) -> bool:
    """The port's three filters, ``None`` meaning "do not filter on this"."""
    if level is not None and record.level.casefold() != level.casefold():
        return False
    if component is not None and record.component != component:
        return False
    return action_id is None or record.action_id == action_id


def core_services_over(
    loop: SidecarLoop,
    *,
    doctor: Callable[[], DoctorReport],
    log_file: Path | None,
    control_wait_s: float = DEFAULT_CONTROL_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> CoreServices:
    """The port bundle for *loop*, over its real subsystems.

    The memory is read off the assembled loop's planner rather than passed in,
    the same way ``build_loop`` verifies its wiring: the memory the port serves
    is provably the memory the planner honours, not one that stopped short.

    ``goals`` is served only over a loop that can actually serve goals — a
    :class:`~pz_agent_core.goals.GoalQueue` *and* a planner the loop can hand
    an activated goal to (:class:`~pz_agent_cli.runtime.GoalPlanner`). Either
    half missing leaves it ``None``, which the router resolves into
    :data:`~pz_agent_mcp.remote.server.NO_GOAL_CHANNEL` at the call site in
    :func:`serve_core_rpc`, where the decision is visible; a channel that
    admitted goals nothing would ever activate is the accepted-and-dropped
    port this module refuses to build.
    """
    planner = loop.planner
    # The navigating wrapper serves navigate_to itself and delegates the rest;
    # the memory-holding planner is the one it wraps.
    unwrapped = unwrap_planner(planner)
    candidate = unwrapped.memory if isinstance(unwrapped, AutonomyPlanner) else None
    memory = candidate if isinstance(candidate, SidecarMemory) else None
    waiter = _ControlWaiter(loop=loop, wait_s=control_wait_s, sleep=sleep, monotonic=monotonic)
    serves_goals = loop.goals is not None and isinstance(planner, GoalPlanner)
    # ``actions`` follows the same rule as ``goals``, decided at this call site
    # where the wiring is visible: served only over a loop that holds the
    # channel its tick actually drains, refused by name otherwise.
    serves_actions = loop.actions is not None
    return CoreServices(
        session=LoopSessionPort(loop=loop, waiter=waiter),
        observations=LoopObservationPort(loop),
        capabilities=LoopCapabilityPort(loop),
        actions=LoopActionPort(loop=loop) if serves_actions else UnservedActionPort(),
        plans=UnservedPlanPort(),
        memory=LoopMemoryPort(memory),
        diagnostics=LoopDiagnosticsPort(doctor_source=doctor, log_file=log_file),
        goals=LoopGoalPort(loop=loop) if serves_goals else None,
    )


def serve_core_rpc(
    supervisor: SidecarSupervisor,
    loop: SidecarLoop,
    *,
    doctor: Callable[[], DoctorReport],
    log_file: Path | None,
    control_wait_s: float = DEFAULT_CONTROL_WAIT_S,
) -> RpcEndpoint:
    """Put the real core behind the published Core RPC endpoint.

    The one serving path: ``pz-agent start --foreground`` calls this after a
    successful attach and before ``loop.run()``, and the end-to-end test calls
    this same function, so what the test proves is what ships. The descriptor
    and token are published and later removed by
    :meth:`~pz_agent_cli.supervisor.SidecarSupervisor.serve_rpc` /
    :meth:`~pz_agent_cli.supervisor.SidecarSupervisor.stop_rpc`; this function
    adds only the router and the adapter.

    Raises:
        SupervisorError: another live sidecar already serves this state
            directory, or this supervisor already serves.
        Exception: whatever binding or publishing raised, unchanged — the
            caller decides whether a sidecar without the link may still run.
    """
    services = core_services_over(
        loop, doctor=doctor, log_file=log_file, control_wait_s=control_wait_s
    )
    router = CoreRouter(services, goals=services.goals or NO_GOAL_CHANNEL)
    return supervisor.serve_rpc(router)
