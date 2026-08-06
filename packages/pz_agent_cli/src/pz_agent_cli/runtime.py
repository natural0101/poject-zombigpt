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

* **Attaching is not arming.** :meth:`SidecarLoop.attach` always comes up in
  ``OBSERVE``, on a fresh session and on a resumed one alike, and there is no
  parameter that changes that. ``session.default_mode`` in ``config.toml`` names
  what :meth:`SidecarLoop.arm` would select; it is not consulted here. The only
  code that sets ``armed`` is :meth:`SidecarLoop.arm`, and the only thing that
  reaches it is an explicit user request — which is additionally refused when it
  was issued before this process attached, so a request file left behind by a
  crashed run cannot re-arm the run that replaces it.

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
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, TypeAlias

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
from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.ipc.journal import JournalReader
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.ipc.snapshot import SnapshotMiss, SnapshotReader
from pz_agent_core.observation.store import DEFAULT_WINDOW, ObservationStore
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


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Every bound the loop runs under. All of them are enforced, not advisory."""

    tick_interval_ms: int = DEFAULT_TICK_INTERVAL_MS
    tick_budget: int = DEFAULT_TICK_BUDGET
    max_actions_per_window: int = DEFAULT_MAX_ACTIONS_PER_WINDOW
    action_window_ms: int = DEFAULT_ACTION_WINDOW_MS
    observations_per_tick: int = DEFAULT_OBSERVATIONS_PER_TICK
    observation_window: int = DEFAULT_WINDOW

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
    ) -> CapabilityRecord:
        """Summarise *report* for the status readout.

        ``verified`` is listed separately from ``usable`` even though every
        verified capability is usable: they are different claims. Usable means
        the symbols are on this install; verified means an action actually ran
        and its postcondition was observed in this session.
        """
        verified = tuple(
            sorted(c.name for c in report.capabilities if c.state is CapabilityState.VERIFIED)
        )
        return cls(
            resolved=True,
            detail=detail,
            build=report.build,
            revision=report.revision,
            usable=report.usable_names(),
            verified=verified,
            refused=dict(report.unusable_reasons()),
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

    def __post_init__(self) -> None:
        if not self.detail:
            raise LoopError("a capability ledger must say how it reached its report, or why not")

    @property
    def resolved(self) -> bool:
        return self.report is not None

    def usable(self, name: str) -> bool:
        """The :data:`~pz_agent_core.actions.engine.CapabilityCheck` for this session."""
        report = self.report
        return report is not None and report.usable(name)

    def record(self) -> CapabilityRecord:
        report = self.report
        if report is None:
            return CapabilityRecord.unresolved(self.detail, notes=self.notes)
        return CapabilityRecord.from_report(report, self.detail, notes=self.notes)

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


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What a whole :meth:`SidecarLoop.run` did."""

    ticks: int
    cause: StopCause
    detail: str
    last: TickOutcome | None = None


@dataclass(frozen=True, slots=True)
class ArmOutcome:
    """Whether an arm or disarm request took effect, and why not if it did not."""

    armed: bool
    mode: SessionMode
    detail: str
    changed: bool = False


@dataclass(frozen=True, slots=True)
class ShutdownOutcome:
    """What shutting down released."""

    lock_released: bool
    detail: str
    lost: tuple[ActionResult, ...] = ()


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
    _attached: _Attached | None = field(default=None, init=False)
    _mode: SessionMode = field(default=SessionMode.OBSERVE, init=False)
    _armed: bool = field(default=False, init=False)
    _tick: int = field(default=0, init=False)
    _budget: ActionBudget = field(init=False)
    _store: ObservationStore = field(init=False)
    _events: deque[SafetyEvent] = field(init=False)

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
        if self.monitor is None:
            self.monitor = HeartbeatMonitor(self.layout, clock=self.clock)
        if self.sessions is None:
            self.sessions = SessionManager(self.layout, clock=self.clock, heartbeats=self.monitor)
        if self.control is None:
            self.control = ControlChannel(self.state_dir / "sidecar.control.json", clock=self.clock)

    # -- accessors ---------------------------------------------------------

    @property
    def mode(self) -> SessionMode:
        return self._mode

    @property
    def armed(self) -> bool:
        """Never True except after an explicit :meth:`arm`. Not configurable."""
        return self._armed

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
    def session(self) -> SessionDescriptor | None:
        return None if self._attached is None else self._attached.session

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
        """
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
            snapshots=SnapshotReader(self.layout),
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
        lost: tuple[ActionResult, ...] = ()
        attached = self._attached
        self._armed = False
        self._mode = SessionMode.OBSERVE
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
        """Grant authority to act. The only thing in this module that sets ``armed``.

        Refused while the panic sentinel is present and while the game is silent:
        arming against a world nothing is reporting on grants authority over
        nothing. The re-arm requirement §3.12 raises after a save change or a
        game restart is cleared *here*, by this explicit call, and nowhere else —
        which is the whole reason it exists as a flag rather than as a timeout.
        """
        attached = self._require_attached()
        if mode not in ARMABLE_MODES:
            return ArmOutcome(
                armed=self._armed,
                mode=self._mode,
                detail=f"{mode.value} is not a mode this build arms into",
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
        refusal = self._multiplayer_refusal()
        if refusal is not None:
            return ArmOutcome(armed=False, mode=self._mode, detail=refusal)
        manager = self._session_manager()
        manager.mark_rearmed()
        self._mode = mode
        self._armed = True
        self._publish_heartbeat()
        return ArmOutcome(
            armed=True,
            mode=mode,
            detail=f"armed in {mode.value} on session {attached.session.session_id}",
            changed=True,
        )

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
        """Drop back to ``OBSERVE``. Always succeeds; disarming is never gated."""
        changed = self._armed or self._mode is not SessionMode.OBSERVE
        self._armed = False
        self._mode = SessionMode.OBSERVE
        if self._attached is not None:
            self._publish_heartbeat()
        return ArmOutcome(
            armed=False,
            mode=SessionMode.OBSERVE,
            detail=f"disarmed: {reason}",
            changed=changed,
        )

    def panic_engaged(self) -> bool:
        """True while the mod's panic sentinel is in the exchange directory."""
        return self.layout.panic_stop.is_file()

    # -- one tick ----------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Read the world, run the guard, and only then consider acting."""
        attached = self._require_attached()
        self._tick += 1
        now = self.clock()
        panic = self.panic_engaged()
        liveness = self._heartbeats().liveness(Peer.GAME, now)
        ingested = attached.feed.drain(self._store)
        # Drained here as well as inside the engine so that a terminal ack for a
        # command nobody is driving any more still frees the backpressure slot.
        attached.sink.poll_acks()

        events = self._run_guard(attached, now, panic=panic, game_alive=liveness.alive)
        lost = self._apply_events(attached, events, now, panic=panic, game_alive=liveness.alive)
        disarmed_by_guard = any(event.forces_disarm for event in events) or panic

        control = self._consume_control(now, attached.attached_at_ms)
        stop = self._apply_control(control, events)
        results = self._act(attached, now, events=events, panic=panic, game_alive=liveness.alive)

        self._publish_heartbeat()
        if self.pid_file is not None:
            self.pid_file.refresh()
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
        )

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
        forcing = sorted({e.reason_code.value for e in events if e.forces_disarm})
        if panic:
            self.disarm(reason="panic stop")
        elif forcing:
            self.disarm(reason=", ".join(forcing))
        elif not game_alive and self._armed:
            self.disarm(reason=ReasonCode.GAME_DISCONNECTED.value)
        return tuple(lost)

    def _consume_control(self, now_ms: int, attached_at_ms: int) -> ControlRequest | None:
        """Take at most one pending request, refusing the ones that prove nothing.

        An **arm** issued before this process attached is refused outright, and
        so is one that has gone stale. That is the mechanism behind "never
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
        channel.clear()
        if request.kind is not ControlKind.ARM:
            return request
        if request.issued_at_ms < attached_at_ms:
            return None
        if now_ms - request.issued_at_ms > CONTROL_MAX_AGE_MS:
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
            return StopCause.STOP_REQUESTED
        if request.kind is ControlKind.DISARM:
            self.disarm(reason="requested")
            return None
        if any(event.forces_disarm for event in events):
            return None
        self.arm(request.mode or SessionMode.ASSISTED)
        return None

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
        """
        if self.planner is None or not self._armed:
            return ()
        if panic or not game_alive or events:
            return ()
        current = self._store.latest()
        if current is None or not current.can_act:
            return ()
        if not self._budget.allows(now_ms):
            return ()
        request = self.planner.propose(current)
        if request is None:
            return ()
        self._budget.spend(now_ms)
        result = attached.engine.execute(request)
        self._confirm(request.action, result)
        return (result,)

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
