"""Sidecar process lifecycle, and the one honest answer to "is the game running?".

Three things live here, and each exists because the obvious version of it is
wrong on Windows:

* **The pid record.** A pid alone cannot answer "is it alive?". ``os.kill(pid, 0)``
  is a probe on POSIX and a call to ``TerminateProcess`` on Win32, so asking the
  operating system about a recycled pid can kill an unrelated process. Liveness
  is therefore decided the same way :mod:`pz_agent_core.session.lock` decides it:
  the holder refreshes a timestamp, and a record that has stopped moving is a
  process that has stopped running. That also keeps "never started" ("there is no
  record") distinguishable from "died" ("there is a record and it went cold").

* **The control channel.** ``stop``, ``arm`` and ``disarm`` are typed by a user
  in a second terminal, so they have to reach a process that is already running.
  A signal cannot carry "arm in ASSISTED", and on Windows a signal cannot even
  carry "stop cleanly". They travel as a single-shot request file the loop reads,
  applies and deletes. Every request carries the instant it was issued, because
  the loop must be able to refuse one that was written before it attached — a
  leftover ``arm`` re-arming a restarted sidecar is precisely the "never re-arms
  itself" rule being broken by a stale file.

* **The game probe.** ``BackupManager.restore`` takes ``game_running`` as a
  required keyword because a wrong answer corrupts a save. The verdict here is
  three-valued on purpose: a fresh game heartbeat proves the game is open, a
  process listing that ran and named no Zomboid process proves it is closed, and
  a listing that could not be produced proves *nothing* — so it reports
  ``may_be_running`` rather than the convenient ``not_running``.
  :func:`game_running_for_restore` collapses that to the boolean the backup
  manager wants, and it collapses toward refusing.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeAlias

from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.protocol import JsonDict, SessionMode
from pz_agent_core.session.heartbeat import PeerLiveness
from pz_agent_core.version import PRODUCT_VERSION

#: Where the running sidecar records itself, under the state directory.
PID_FILE_NAME: Final = "sidecar.pid.json"

#: The single-shot request file ``arm``/``disarm``/``stop`` write.
CONTROL_FILE_NAME: Final = "sidecar.control.json"

#: Where a detached sidecar's stdout and stderr go. Nothing parses it; it is the
#: only place a traceback from a process with no terminal can land.
SPAWN_LOG_NAME: Final = "sidecar.out"

#: A pid record that has not been refreshed for this long belongs to a process
#: that is no longer ticking. Three of the loop's five-second heartbeat periods,
#: matching :data:`pz_agent_core.session.lock.DEFAULT_STALE_AFTER_MS` so the two
#: staleness judgements cannot disagree about the same process.
DEFAULT_STALE_AFTER_MS: Final = 15_000

#: Ceiling on the bytes read from a process listing. A machine cannot have
#: enough processes to fill this; a command that hangs printing garbage can.
MAX_PROCESS_OUTPUT_BYTES: Final = 1024 * 1024

#: Ceiling on the process names retained from one listing.
MAX_PROCESSES: Final = 4096

#: Seconds the process listing may take before it is abandoned as unusable.
PROCESS_LIST_TIMEOUT_S: Final = 5.0

#: Matched case-insensitively against every process name. Deliberately a
#: substring of the executable rather than an exact filename: the launcher is
#: ``ProjectZomboid64.exe`` on one install, ``ProjectZomboid32.exe`` on another
#: and ``ProjectZomboid64`` on Linux, and a name this misses is a save this tool
#: would have let somebody overwrite.
GAME_PROCESS_MARKER: Final = "zomboid"

#: A control request older than this is ignored. It is not evidence of what the
#: user wants now: they typed it, walked away, and the sidecar has since done
#: something else — most importantly, it may have restarted into OBSERVE.
CONTROL_MAX_AGE_MS: Final = 30_000

#: ``(argv, cwd, log path) -> pid``. Injected so a test drives ``start`` without
#: creating a real process.
Spawner: TypeAlias = Callable[[Sequence[str], Path, Path], int]

#: ``(pid, signal number) -> None``. Injected for the same reason.
Signaller: TypeAlias = Callable[[int, int], None]

#: ``() -> ProcessListing``. Injected so the game probe is testable on a machine
#: that has neither ``tasklist`` nor a running game.
ProcessLister: TypeAlias = Callable[[], "ProcessListing"]


class SupervisorError(ValueError):
    """A supervisor record is malformed, or an operation was made out of order."""


class SpawnDiedError(SupervisorError):
    """The child was started and was gone again before anyone could use it.

    Carries the exit code and the tail of the spawn log because those two are
    the whole diagnosis, and the operator would otherwise be told that a start
    failed with nothing to act on.
    """

    def __init__(self, *, pid: int, exit_code: int, log_tail: str) -> None:
        detail = f"the sidecar exited immediately (pid {pid}, exit code {exit_code})"
        if log_tail:
            detail += f". It wrote: {log_tail}"
        super().__init__(detail)
        self.pid = pid
        self.exit_code = exit_code
        self.log_tail = log_tail


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Whole-document write with a scratch file, for the state directory.

    :func:`pz_agent_core.ipc.atomic.write_json_atomic` is not usable here: it
    refuses any path an :class:`~pz_agent_core.ipc.layout.IpcLayout` does not
    own, and these files deliberately live outside the exchange directory so
    that removing the mod does not remove the record of a running sidecar.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    try:
        with scratch.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> JsonDict | None:
    """Read one JSON object, or None when there is nothing usable to read."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SupervisorError(f"field {key!r} must be an integer")
    return value


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SupervisorError(f"field {key!r} must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# the pid record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PidRecord:
    """Which process is the sidecar, and when it last said it was still there."""

    pid: int
    started_at_ms: int
    refreshed_at_ms: int
    version: str = PRODUCT_VERSION

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise SupervisorError(f"pid must be positive, got {self.pid}")
        if self.started_at_ms < 0 or self.refreshed_at_ms < 0:
            raise SupervisorError("pid record timestamps must be non-negative")

    def age_ms(self, now_ms: int) -> int:
        """How long since the last refresh; never negative.

        Clamped for the same reason a heartbeat's age is: a record written by a
        process whose clock ran slightly ahead is skew, not freshness that
        outlives a subsequent stall.
        """
        return max(0, now_ms - self.refreshed_at_ms)

    def is_stale(self, now_ms: int, stale_after_ms: int = DEFAULT_STALE_AFTER_MS) -> bool:
        if stale_after_ms <= 0:
            raise SupervisorError(f"stale_after_ms must be positive, got {stale_after_ms}")
        return self.age_ms(now_ms) > stale_after_ms

    def to_dict(self) -> JsonDict:
        return {
            "pid": self.pid,
            "started_at_ms": self.started_at_ms,
            "refreshed_at_ms": self.refreshed_at_ms,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PidRecord:
        return cls(
            pid=_integer(payload, "pid"),
            started_at_ms=_integer(payload, "started_at_ms"),
            refreshed_at_ms=_integer(payload, "refreshed_at_ms"),
            version=_text(payload, "version"),
        )


@dataclass
class PidFile:
    """The sidecar's own record of itself, refreshed while it ticks."""

    path: Path
    clock: Clock = system_clock_ms

    def read(self) -> PidRecord | None:
        """The recorded process, or None when there is no usable record.

        An unparseable file reads as "no record" rather than as a live process:
        it names nobody, so nobody could ever refresh or clear it, and treating
        it as a holder would make ``start`` refuse forever.
        """
        payload = _read_json(self.path)
        if payload is None:
            return None
        try:
            return PidRecord.from_dict(payload)
        except SupervisorError:
            return None

    def claim(self, pid: int, *, version: str = PRODUCT_VERSION) -> PidRecord:
        """Record *pid* as the sidecar, starting its refresh clock now."""
        now = self.clock()
        record = PidRecord(pid=pid, started_at_ms=now, refreshed_at_ms=now, version=version)
        _write_json(self.path, record.to_dict())
        return record

    def refresh(self) -> PidRecord | None:
        """Prove the recorded process is still ticking.

        Returns None when the record has been taken over or removed. Rewriting
        it in that case would let two sidecars each believe they are the one on
        record, which is the state ``start`` exists to prevent.
        """
        current = self.read()
        if current is None or current.pid != os.getpid():
            return None
        refreshed = PidRecord(
            pid=current.pid,
            started_at_ms=current.started_at_ms,
            refreshed_at_ms=self.clock(),
            version=current.version,
        )
        _write_json(self.path, refreshed.to_dict())
        return refreshed

    def clear(self) -> bool:
        """Remove the record. Returns whether there was one."""
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed


# ---------------------------------------------------------------------------
# the control channel
# ---------------------------------------------------------------------------


class ControlKind(StrEnum):
    """What a second terminal is asking the running loop to do."""

    ARM = "arm"
    DISARM = "disarm"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """One single-shot instruction for the running loop.

    ``issued_at_ms`` and ``nonce`` are both load-bearing. The timestamp lets the
    loop refuse a request written before it attached, so a restarted sidecar
    cannot be armed by a file left behind by the previous run. The nonce makes
    two identical requests distinguishable in a log, which is the difference
    between "the user asked twice" and "the file was never consumed".
    """

    kind: ControlKind
    issued_at_ms: int
    nonce: str
    mode: SessionMode | None = None

    def __post_init__(self) -> None:
        if self.issued_at_ms < 0:
            raise SupervisorError(f"issued_at_ms must be non-negative, got {self.issued_at_ms}")
        if not self.nonce:
            raise SupervisorError("a control request must carry a nonce")
        if self.kind is not ControlKind.ARM and self.mode is not None:
            raise SupervisorError(f"only an arm request names a mode, not {self.kind.value}")
        if self.mode is not None and self.mode is SessionMode.OBSERVE:
            # OBSERVE is what disarming means. Accepting it as an arm target
            # would make "arm" a no-op that still reports success.
            raise SupervisorError("arming into OBSERVE is a disarm; use the disarm request")

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "kind": self.kind.value,
            "issued_at_ms": self.issued_at_ms,
            "nonce": self.nonce,
        }
        if self.mode is not None:
            out["mode"] = self.mode.value
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ControlRequest:
        raw_kind = _text(payload, "kind")
        try:
            kind = ControlKind(raw_kind)
        except ValueError as exc:
            raise SupervisorError(f"unknown control request {raw_kind!r}") from exc
        raw_mode = payload.get("mode")
        if raw_mode is None:
            mode = None
        else:
            if not isinstance(raw_mode, str):
                raise SupervisorError("control field 'mode' must be a string")
            try:
                mode = SessionMode(raw_mode)
            except ValueError as exc:
                raise SupervisorError(f"unknown session mode {raw_mode!r}") from exc
        return cls(
            kind=kind,
            issued_at_ms=_integer(payload, "issued_at_ms"),
            nonce=_text(payload, "nonce"),
            mode=mode,
        )


@dataclass
class ControlChannel:
    """One request file, written by a command and consumed by the loop."""

    path: Path
    clock: Clock = system_clock_ms

    def write(self, kind: ControlKind, *, mode: SessionMode | None = None) -> ControlRequest:
        """Publish a request, replacing any that has not been consumed yet.

        Replacing rather than queueing is deliberate: the loop applies at most
        one request per tick, and a user who typed ``disarm`` after ``arm``
        means the second one — not both, in order, a second later.
        """
        request = ControlRequest(
            kind=kind, issued_at_ms=self.clock(), nonce=uuid.uuid4().hex, mode=mode
        )
        _write_json(self.path, request.to_dict())
        return request

    def read(self) -> ControlRequest | None:
        """The pending request, or None when there is none that parses."""
        payload = _read_json(self.path)
        if payload is None:
            return None
        try:
            return ControlRequest.from_dict(payload)
        except SupervisorError:
            return None

    def clear(self) -> bool:
        """Consume the pending request. Returns whether a file was removed."""
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed

    def pending(self) -> bool:
        return self.path.exists()


# ---------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------


class SupervisorState(StrEnum):
    """What the pid record says about the sidecar right now."""

    NEVER_STARTED = "never_started"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    """The verdict, with the evidence it was reached from."""

    state: SupervisorState
    detail: str
    record: PidRecord | None = None
    control_pending: bool = False

    @property
    def alive(self) -> bool:
        return self.state is SupervisorState.RUNNING

    def to_dict(self) -> JsonDict:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "alive": self.alive,
            "control_pending": self.control_pending,
            "record": None if self.record is None else self.record.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class StartOutcome:
    """What ``start`` did."""

    started: bool
    detail: str
    record: PidRecord | None = None


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """What ``stop`` did.

    ``requested`` and ``signalled`` are separate because they are separate
    facts: the request file always lands, and a signal is only sent where one
    can be delivered without killing the process outright.
    """

    requested: bool
    detail: str
    signalled: bool = False
    record: PidRecord | None = None


#: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``. Read through :func:`getattr`
#: rather than named directly so this module still type-checks and imports on
#: the POSIX box that runs CI, where neither constant exists.
_DETACHED_FLAGS: Final[int] = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)


#: Handles for children this process deliberately outlives. See the note in
#: :func:`_default_spawner`; nothing reads this list.
_DETACHED_CHILDREN: Final[list[subprocess.Popen[bytes]]] = []

#: How long to watch a freshly spawned sidecar before calling it started.
#: Short enough to be imperceptible, long enough to catch the failures that
#: happen at once — a bad interpreter, a missing module, an unreadable config.
SPAWN_GRACE_S: Final = 0.3

#: Bytes of the spawn log quoted back when a child dies immediately. Enough for
#: a Python traceback's last line, which is the part that names the cause.
SPAWN_LOG_TAIL: Final = 400


def _default_spawner(argv: Sequence[str], cwd: Path, log_path: Path) -> int:
    """Start a detached child, confirm it is still there, and return its pid.

    Detached rather than backgrounded: the sidecar must survive the terminal
    that launched it closing, which is the normal way a user starts it before
    launching the game.

    The confirmation is here rather than in :meth:`SidecarSupervisor.start`
    because this is the only place that still holds the process handle — once
    the pid is returned the child is detached and there is no portable way to
    ask after it. ``os.kill(pid, 0)`` is not that way: on Windows CPython maps
    it to ``TerminateProcess``, so the liveness check would kill the thing it
    was checking.

    Reporting a start on the strength of ``Popen`` returning is reporting that
    a *fork* succeeded. It says nothing about whether the program ran, and this
    project's own rule is that success means an observed postcondition. A
    sidecar that died on its first import used to leave ``start`` printing
    "sidecar started as pid N" and exiting 0, after which ``arm`` failed for
    reasons that named nothing.

    What is observed is bounded and stated plainly: the process is still alive
    ``SPAWN_GRACE_S`` after it was spawned. A child that dies later is a
    different failure, and ``status`` is what reports that one.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if os.name == "nt":
        extra["creationflags"] = _DETACHED_FLAGS
    else:
        extra["start_new_session"] = True
    with log_path.open("ab") as sink:
        # argv is composed by the caller from this package's own entry point and
        # resolved paths; nothing from the wire or from an LLM reaches it, and
        # there is no shell between here and the executable.
        process = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **extra,
        )
    try:
        exited = process.wait(timeout=SPAWN_GRACE_S)
    except subprocess.TimeoutExpired:
        # Still running after the grace, which is what "started" means here.
        #
        # The handle is kept because abandoning it makes Python emit a
        # ResourceWarning at collection — correctly, since a child that was
        # never waited on is a child nobody reaped. Abandoning it is the
        # intent: this process exits moments later and the sidecar must outlive
        # it. Holding the reference until then says so, rather than leaving a
        # warning for someone to explain away.
        _DETACHED_CHILDREN.append(process)
        return process.pid
    raise SpawnDiedError(pid=process.pid, exit_code=exited, log_tail=_log_tail(log_path))


def _log_tail(log_path: Path) -> str:
    """The end of the spawn log, for a child that had something to say.

    Read on the failure path only. A child that died on an import wrote the
    reason here and nowhere else, and an operator told "it did not start" with
    no further detail has to go looking for a file they do not know about.
    """
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - SPAWN_LOG_TAIL))
            return handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _default_signaller(pid: int, signal_number: int) -> None:
    os.kill(pid, signal_number)


@dataclass
class SidecarSupervisor:
    """Starts, stops and reports on the sidecar process.

    ``signals_supported`` is False on Windows and cannot be talked into being
    True there by configuration: ``os.kill`` on Win32 terminates rather than
    signals, and a "graceful stop" that kills the process mid-write is not one.
    The control file is the stop path on every platform; the signal, where it
    exists, only shortens the wait.
    """

    state_dir: Path
    clock: Clock = system_clock_ms
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS
    spawn: Spawner = _default_spawner
    signaller: Signaller = _default_signaller
    signals_supported: bool = os.name != "nt"
    stop_signal: int = int(signal.SIGTERM)
    _pid_file: PidFile = field(init=False)
    _control: ControlChannel = field(init=False)

    def __post_init__(self) -> None:
        if self.stale_after_ms <= 0:
            raise SupervisorError(f"stale_after_ms must be positive, got {self.stale_after_ms}")
        self._pid_file = PidFile(self.state_dir / PID_FILE_NAME, clock=self.clock)
        self._control = ControlChannel(self.state_dir / CONTROL_FILE_NAME, clock=self.clock)

    @property
    def pid_file(self) -> PidFile:
        return self._pid_file

    @property
    def control(self) -> ControlChannel:
        return self._control

    @property
    def spawn_log(self) -> Path:
        return self.state_dir / SPAWN_LOG_NAME

    def status(self, now_ms: int | None = None) -> SupervisorStatus:
        """Report whether a sidecar is actually running.

        Three outcomes, never two: no record at all is *not* the same as a
        record that went cold, and collapsing them would tell a user whose
        sidecar crashed that they never started one.
        """
        moment = self.clock() if now_ms is None else now_ms
        record = self._pid_file.read()
        pending = self._control.pending()
        if record is None:
            return SupervisorStatus(
                state=SupervisorState.NEVER_STARTED,
                detail="no sidecar has recorded itself in this state directory",
                control_pending=pending,
            )
        age = record.age_ms(moment)
        if record.is_stale(moment, self.stale_after_ms):
            return SupervisorStatus(
                state=SupervisorState.STOPPED,
                detail=(
                    f"pid {record.pid} last refreshed {age} ms ago, past the "
                    f"{self.stale_after_ms} ms threshold; it is no longer ticking"
                ),
                record=record,
                control_pending=pending,
            )
        return SupervisorStatus(
            state=SupervisorState.RUNNING,
            detail=f"pid {record.pid} refreshed {age} ms ago",
            record=record,
            control_pending=pending,
        )

    def start(self, argv: Sequence[str], *, cwd: Path | None = None) -> StartOutcome:
        """Spawn a detached sidecar and record its pid.

        Refuses while one is already running. Two sidecars on one exchange
        directory interleave the command stream; the :class:`SidecarLock` would
        catch it a moment later, but refusing here means the second process is
        never started rather than started and immediately wedged.
        """
        if not argv:
            raise SupervisorError("start requires a command line")
        current = self.status()
        if current.state is SupervisorState.RUNNING:
            return StartOutcome(
                started=False,
                detail=f"a sidecar is already running: {current.detail}",
                record=current.record,
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # A stale request from the previous run must not be the first thing the
        # new loop reads. It would be refused for being older than the attach,
        # but clearing it here keeps that refusal off the happy path.
        self._control.clear()
        try:
            pid = self.spawn(list(argv), cwd or self.state_dir, self.spawn_log)
        except SpawnDiedError as died:
            # No pid is claimed. A record for a process that is already gone
            # would make `status` report STOPPED rather than NEVER_STARTED, and
            # "it crashed" is a different thing to tell someone than "it never
            # ran" — which is the distinction status exists to make.
            return StartOutcome(started=False, detail=str(died))
        record = self._pid_file.claim(pid)
        return StartOutcome(
            started=True,
            detail=f"sidecar started as pid {pid}",
            record=record,
        )

    def request_stop(self) -> StopOutcome:
        """Ask the running sidecar to shut down.

        The request file is written first and unconditionally: it is the only
        stop path that works on Windows, and it is the only one that lets the
        loop release its lock and close in-flight work on the way out. The
        signal is an accelerator, not the mechanism.
        """
        status = self.status()
        if status.state is SupervisorState.NEVER_STARTED:
            return StopOutcome(
                requested=False,
                detail="no sidecar has recorded itself in this state directory",
            )
        self._control.write(ControlKind.STOP)
        if status.state is SupervisorState.STOPPED:
            return StopOutcome(
                requested=True,
                detail=(
                    f"the recorded sidecar is not ticking ({status.detail}); "
                    "a stop request was left for it and the record cleared"
                ),
                record=status.record,
            )
        record = status.record
        signalled = False
        detail = f"stop requested of pid {record.pid}" if record is not None else "stop requested"
        if record is not None and self.signals_supported:
            try:
                self.signaller(record.pid, self.stop_signal)
                signalled = True
            except (OSError, ValueError) as exc:
                # The pid may have exited between the status read and here, or
                # been recycled to a process this user may not signal. Neither is
                # a failure of the stop: the request file is already down.
                detail = f"{detail}; the signal could not be delivered ({exc})"
        return StopOutcome(requested=True, detail=detail, signalled=signalled, record=record)

    def arm(self, mode: SessionMode) -> ControlRequest:
        """Publish an arm request for the running loop to apply."""
        return self._control.write(ControlKind.ARM, mode=mode)

    def disarm(self) -> ControlRequest:
        """Publish a disarm request for the running loop to apply."""
        return self._control.write(ControlKind.DISARM)

    def forget(self) -> bool:
        """Drop the pid record and any pending request. Returns whether one existed."""
        cleared = self._pid_file.clear()
        self._control.clear()
        return cleared


# ---------------------------------------------------------------------------
# is the game running?
# ---------------------------------------------------------------------------


class GameRunning(StrEnum):
    """The three answers a process probe can honestly give."""

    RUNNING = "running"
    MAY_BE_RUNNING = "may_be_running"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class ProcessListing:
    """The process names this machine reported, or why it reported none."""

    names: tuple[str, ...] = ()
    problem: str = ""
    truncated: bool = False

    @property
    def usable(self) -> bool:
        """True when the listing is evidence about what is running.

        A truncated listing is still usable *for finding* a process and useless
        for concluding one is absent; :func:`probe_game_running` is what applies
        that asymmetry.
        """
        return not self.problem


@dataclass(frozen=True, slots=True)
class GameRunningProbe:
    """Whether Project Zomboid is open, and what that conclusion rests on."""

    verdict: GameRunning
    detail: str
    matches: tuple[str, ...] = ()

    @property
    def unsafe_to_restore(self) -> bool:
        """True unless the game was positively shown to be closed.

        ``may_be_running`` lands here with ``running``: restoring a save under a
        live game corrupts it, and "we could not tell" is not permission.
        """
        return self.verdict is not GameRunning.NOT_RUNNING

    def to_dict(self) -> JsonDict:
        return {
            "verdict": self.verdict.value,
            "detail": self.detail,
            "matches": list(self.matches),
            "unsafe_to_restore": self.unsafe_to_restore,
        }


def _listing_argv() -> tuple[str, ...]:
    """The fixed command line for this platform's process listing."""
    if os.name == "nt":
        return ("tasklist.exe", "/FO", "CSV", "/NH")
    return ("ps", "-A", "-o", "comm=")


def _parse_tasklist(text: str) -> tuple[str, ...]:
    """First column of ``tasklist /FO CSV``, which is the image name.

    Parsed as CSV rather than split on commas: the memory column is written
    ``"100,000 K"`` in most locales, and a naive split makes every row ragged.
    """
    names: list[str] = []
    for row in csv.reader(text.splitlines()):
        if not row or not row[0].strip():
            continue
        names.append(row[0].strip())
        if len(names) >= MAX_PROCESSES:
            break
    return tuple(names)


def _parse_ps(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        names.append(stripped)
        if len(names) >= MAX_PROCESSES:
            break
    return tuple(names)


def list_processes(*, timeout_s: float = PROCESS_LIST_TIMEOUT_S) -> ProcessListing:
    """Enumerate process names, or say why they could not be enumerated.

    Every failure — the tool is missing, it exited non-zero, it hung, its output
    is not decodable — produces a *problem* rather than an empty listing. An
    empty listing means "nothing is running", and that claim is the one that
    would let a restore proceed against an open game.
    """
    argv = _listing_argv()
    try:
        # Fixed argv, no shell, bounded time. The only external command this
        # project runs, and it runs it to avoid guessing about a live game.
        completed = subprocess.run(  # noqa: S603
            list(argv),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return ProcessListing(problem=f"{argv[0]} is not available on this machine")
    except subprocess.TimeoutExpired:
        return ProcessListing(problem=f"{argv[0]} did not finish within {timeout_s:g} s")
    except OSError as exc:
        return ProcessListing(problem=f"{argv[0]} could not be run ({exc.strerror or exc})")
    if completed.returncode != 0:
        return ProcessListing(problem=f"{argv[0]} exited with status {completed.returncode}")
    raw = completed.stdout or b""
    truncated = len(raw) > MAX_PROCESS_OUTPUT_BYTES
    text = raw[:MAX_PROCESS_OUTPUT_BYTES].decode("utf-8", errors="replace")
    names = _parse_tasklist(text) if os.name == "nt" else _parse_ps(text)
    if len(names) >= MAX_PROCESSES:
        truncated = True
    if not names:
        return ProcessListing(problem=f"{argv[0]} listed no processes at all")
    return ProcessListing(names=names, truncated=truncated)


def probe_game_running(
    *,
    heartbeat: PeerLiveness | None = None,
    lister: ProcessLister = list_processes,
) -> GameRunningProbe:
    """Decide whether Project Zomboid is open, conservatively.

    Order matters. A live game heartbeat is proof and is checked first, because
    it needs no external command and cannot be defeated by a restricted
    environment. Only when there is no such proof does the probe fall back to
    the process table, and only a listing that *ran* may be read as an absence.
    """
    if heartbeat is not None and heartbeat.alive:
        return GameRunningProbe(
            verdict=GameRunning.RUNNING,
            detail="the mod is writing a game heartbeat, so the game is open",
        )
    listing = lister()
    if not listing.usable:
        return GameRunningProbe(
            verdict=GameRunning.MAY_BE_RUNNING,
            detail=(
                f"the process list could not be read ({listing.problem}), and there is no "
                "game heartbeat either — the game may be running"
            ),
        )
    matches = tuple(name for name in listing.names if GAME_PROCESS_MARKER in name.casefold())
    if matches:
        return GameRunningProbe(
            verdict=GameRunning.RUNNING,
            detail=f"{len(matches)} running process(es) name Project Zomboid",
            matches=matches,
        )
    if listing.truncated:
        return GameRunningProbe(
            verdict=GameRunning.MAY_BE_RUNNING,
            detail=(
                "the process list was truncated before it ended, so the absence of a "
                "Project Zomboid process proves nothing"
            ),
        )
    return GameRunningProbe(
        verdict=GameRunning.NOT_RUNNING,
        detail=(
            f"no game heartbeat, and none of the {len(listing.names)} running "
            "process(es) is Project Zomboid"
        ),
    )


def game_running_for_restore(probe: GameRunningProbe) -> bool:
    """The boolean :meth:`BackupManager.restore` demands, biased toward refusing.

    A separate function rather than a property so that the direction of the
    collapse is stated at the call site: everything except a positive "closed"
    counts as running, and a restore is refused.
    """
    return probe.unsafe_to_restore
