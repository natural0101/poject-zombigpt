"""The bridge itself: a child process, two pipes, and everything bounded.

This is the concrete implementation of
:class:`~pz_agent_voice.adapters.teamon.TeamONClient`. The vendor SDK is not
installed here and its surface is unverified, so the integration point this
build ships is a *process boundary*: a small program — the bridge — links
against the SDK, speaks the JSONL protocol in :mod:`.protocol` on stdin and
stdout, and this module supervises it.

That boundary is the design, not a workaround. A vendor SDK loaded in-process
gets the sidecar's memory, its exception handling and its exit code; a vendor
SDK behind a pipe gets a fixed byte budget and a kill switch. Everything here
follows from that:

**Two threads, never the caller's.** A reader drains stdout into bounded queues
and a writer drains a bounded outbox into stdin. A caller that wants to send
something puts it in a queue and returns, so a bridge that has stopped reading
its stdin cannot wedge the companion's event loop — the pipe fills, the writer
thread blocks, and the outbox refuses instead of growing.

**The stop path never waits for agreement.** :meth:`JsonlBridge.stop` signals,
waits ``stop_timeout``, kills, waits ``stop_timeout`` again and gives up — at
most twice a bound the caller set, whatever the bridge does. It never joins a
thread without a timeout and never waits on a pipe. ``terminate`` is the one
shutdown signal both platforms have; a graceful "close stdin and let it exit"
would be graceful on POSIX, which this does not ship on, and absent on Windows,
which it does.

**Restarts are counted.** A crashed bridge is relaunched on the next call, up to
``max_restarts``; past that the bridge is dead, the state says so, and a report
goes to the listener carrying a sentence for the user. A dead bridge that
reported nothing would leave the user talking to a machine that stopped
listening some minutes ago.

**No credential lives here.** :class:`BridgeConfig` refuses a field whose name
looks like a secret, in the mapping it is loaded from, in the environment it
passes on and in the command line it launches — and the child's environment is
an allowlist, so the parent's own secrets do not travel either. Whatever the
vendor SDK needs for authentication, the bridge program reads for itself, from
somewhere this process never touches.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from shutil import which
from types import MappingProxyType
from typing import Final, TypeAlias

from ..adapters.teamon import TeamONSpeech, TeamONTranscript
from ..messages import VoiceGoal
from .protocol import (
    BRIDGE_UNAVAILABLE_PHRASE,
    BridgeFault,
    BridgeFaultCode,
    BridgeMessage,
    BridgeMessageType,
    BridgeOutcome,
    BridgeProtocolError,
    BridgeTranscript,
    LineFramer,
    OutcomeStatus,
    OverlongLine,
    ProtocolMismatch,
    UnknownMessageType,
    decode,
    encode,
    goal_message,
    hello_message,
    interrupt_message,
    read_error,
    read_outcome,
    read_ready,
    read_transcript,
    speak_message,
)

__all__ = [
    "MAX_TIMEOUT_SECONDS",
    "BridgeCheck",
    "BridgeConfig",
    "BridgeError",
    "BridgeFailed",
    "BridgeListener",
    "BridgeReason",
    "BridgeReport",
    "BridgeState",
    "BridgeTimeout",
    "BridgeUnavailable",
    "JsonlBridge",
    "TeamONBridgeClient",
    "check_bridge",
]

#: Ceiling on every configurable wait. A timeout is a bound; a bound of an hour
#: is a bound in name only, and the companion is a real-time thing — nothing it
#: does is worth a minute of silence.
MAX_TIMEOUT_SECONDS: Final = 60.0

#: How much of stdout is taken per read. One page: large enough that a chatty
#: recogniser is not a syscall per word, small enough to be a fixed cost.
_CHUNK_BYTES: Final = 4096

#: How long :meth:`TeamONBridgeClient.transcripts` parks in a worker thread
#: before looping. It is not a timeout on anything — it is how quickly the
#: stream notices that the bridge died or that the session was closed.
_TRANSCRIPT_POLL_SECONDS: Final = 0.2

#: The child gets these and nothing else. The parent's environment holds the
#: planner's API key, the RPC token's path and whatever else the user's shell
#: exports; none of it is the bridge's business, and a child that inherits a
#: secret it never needed is a secret in one more process's memory.
_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "TZ",
)

#: Substrings that make a configuration key a credential as far as this module
#: is concerned. Deliberately broad: a false positive costs a rename, and a
#: false negative puts a key in a file that ends up in a support bundle.
_CREDENTIAL_WORDS: Final[tuple[str, ...]] = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "auth",
    "bearer",
)

_ENV_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: What a refused or failed synthesis becomes. Named on this side rather than
#: taken from whatever the bridge wrote about it: the outcome already says the
#: sentence was not spoken, and the words it used to say so are its own.
_SYNTHESIS_FAULT: Final = BridgeFault(code=BridgeFaultCode.SYNTHESIS)

#: Configuration keys :meth:`BridgeConfig.from_mapping` knows. Anything else is
#: refused rather than ignored — a misspelled timeout that silently keeps the
#: default is a bound the user believes they set.
_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "command",
        "env",
        "startup_timeout",
        "reply_timeout",
        "speech_timeout",
        "stop_timeout",
        "max_restarts",
        "max_pending",
    }
)


class BridgeState(StrEnum):
    """Where the supervisor thinks the bridge is.

    ``RUNNING`` means "started, and not stopped or dead" — it is the intent, not
    a liveness claim, because the child can exit between any two statements.
    :attr:`JsonlBridge.alive` is the liveness claim.
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEAD = "dead"


class BridgeReason(StrEnum):
    """Why a report was made. A closed set, so a listener can switch on it."""

    LAUNCH_FAILED = "launch_failed"
    HANDSHAKE_FAILED = "handshake_failed"
    VERSION_MISMATCH = "version_mismatch"
    CRASHED = "crashed"
    RESTARTED = "restarted"
    RESTART_LIMIT = "restart_limit"
    OVERLONG_LINE = "overlong_line"
    MALFORMED_LINE = "malformed_line"
    UNKNOWN_TYPE = "unknown_type"
    DROPPED = "dropped"
    FAULT = "fault"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """One thing about the bridge worth telling a log, and sometimes the user.

    ``detail`` is a sentence written in this module, for a log. The only thing
    from the far end that can appear in one is a bounded token quoted by a
    protocol refusal — an unrecognised message type name, cut to 32 characters.
    A transcript never reaches it, a fault's ``detail`` never reaches it, and
    :meth:`spoken` never returns it: what the user hears comes from the closed
    phrase table, not from a diagnostic.
    """

    state: BridgeState
    reason: BridgeReason
    detail: str
    fault: BridgeFault | None = None

    def spoken(self) -> str | None:
        """The sentence the user hears, or ``None`` when this is log-only.

        Most reports are diagnostics — a malformed line, a restart that worked —
        and narrating them would be an assistant reading out its own log. The
        two the user has to know about are a fault the bridge reported and a
        bridge that is not coming back.
        """
        if self.fault is not None:
            return self.fault.summary()
        if self.state is BridgeState.DEAD:
            return BRIDGE_UNAVAILABLE_PHRASE
        return None


#: Where reports go. Synchronous and called from the reader thread, so an
#: implementation must not block.
BridgeListener: TypeAlias = Callable[[BridgeReport], None]


class BridgeError(RuntimeError):
    """Something went wrong with the bridge, in a way the caller must see."""


class BridgeUnavailable(BridgeError):
    """The bridge is not there: never launched, crashed for good, or stopped."""


class BridgeTimeout(BridgeError):
    """The bridge is running and did not answer in time."""


class BridgeFailed(BridgeError):
    """The bridge reported a failure of its own.

    The message is the fault's summary from the closed table in :mod:`.protocol`
    — never the ``detail`` the bridge sent, which is dropped at decode time.
    """

    def __init__(self, fault: BridgeFault) -> None:
        super().__init__(fault.summary())
        self.fault = fault


def _is_credential_name(name: str) -> bool:
    folded = name.casefold()
    return any(word in folded for word in _CREDENTIAL_WORDS)


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """How to launch the bridge, and every bound it runs under.

    There is no credential field, and there is no way to add one: a name that
    looks like a secret is refused wherever it can appear. That is a real
    constraint on the bridge program rather than a gesture — whatever it needs
    to authenticate, it reads itself, and this process stays a component that
    holds no key and therefore cannot leak one.
    """

    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: The handshake. Short: a bridge that cannot say hello in five seconds is
    #: not a bridge that will keep up with speech.
    startup_timeout: float = 5.0
    #: One control answer — an outcome for a dispatched goal.
    reply_timeout: float = 5.0
    #: One spoken sentence, delivered. Longer than a control answer because it
    #: covers a synthesiser actually reading two hundred characters aloud.
    speech_timeout: float = 30.0
    #: Half the stop budget: ``stop`` waits this long after asking, then kills
    #: and waits it once more.
    stop_timeout: float = 1.0
    max_restarts: int = 3
    #: Depth of the outbox and of each inbound queue. A queue this deep already
    #: means the far end is not keeping up; deeper would only mean staler.
    max_pending: int = 64

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("a bridge configuration must name a command to launch")
        for token in self.command:
            if not token:
                raise ValueError("a bridge command may not contain an empty argument")
            if _is_credential_name(token):
                raise ValueError(
                    "a bridge command line may not carry a credential; the bridge program "
                    f"reads its own, and {token.split('=')[0]!r} looks like one"
                )
        for name in self.env:
            if not _ENV_NAME.match(name):
                raise ValueError(f"{name!r} is not a usable environment variable name")
            if _is_credential_name(name):
                raise ValueError(
                    f"the bridge environment may not carry a credential, and {name!r} "
                    "looks like one; the bridge program reads its own"
                )
        for label, value in (
            ("startup_timeout", self.startup_timeout),
            ("reply_timeout", self.reply_timeout),
            ("speech_timeout", self.speech_timeout),
            ("stop_timeout", self.stop_timeout),
        ):
            if not 0 < value <= MAX_TIMEOUT_SECONDS:
                raise ValueError(
                    f"{label} must be within 0..{MAX_TIMEOUT_SECONDS:g} seconds, got {value}"
                )
        if not 0 <= self.max_restarts <= 10:
            raise ValueError(f"max_restarts must be within 0..10, got {self.max_restarts}")
        if not 1 <= self.max_pending <= 1024:
            raise ValueError(f"max_pending must be within 1..1024, got {self.max_pending}")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> BridgeConfig:
        """Build a configuration from parsed TOML or JSON.

        Raises:
            ValueError: an unknown key, a key that looks like a credential, or a
                value of the wrong shape. Unknown keys are refused rather than
                ignored so that a misspelling is a failure to start rather than
                a bound the user believes they configured.
        """
        for key in data:
            if _is_credential_name(key):
                raise ValueError(
                    f"the bridge configuration may not carry a credential, and {key!r} "
                    "looks like one; the bridge program reads its own"
                )
            if key not in _CONFIG_KEYS:
                known = ", ".join(sorted(_CONFIG_KEYS))
                raise ValueError(f"{key!r} is not a bridge setting; known settings are {known}")
        raw_command = data.get("command")
        if not isinstance(raw_command, (list, tuple)) or not all(
            isinstance(token, str) for token in raw_command
        ):
            raise ValueError("'command' must be a list of strings")
        env = _string_mapping(data.get("env", {}))
        return cls(
            command=tuple(str(token) for token in raw_command),
            env=MappingProxyType(env),
            startup_timeout=_number(data, "startup_timeout", 5.0),
            reply_timeout=_number(data, "reply_timeout", 5.0),
            speech_timeout=_number(data, "speech_timeout", 30.0),
            stop_timeout=_number(data, "stop_timeout", 1.0),
            max_restarts=int(_number(data, "max_restarts", 3)),
            max_pending=int(_number(data, "max_pending", 64)),
        )

    def child_environment(self) -> dict[str, str]:
        """Exactly what the child process gets: an allowlist plus ``env``."""
        inherited = {
            name: os.environ[name] for name in _ENV_ALLOWLIST if os.environ.get(name) is not None
        }
        inherited.update(self.env)
        return inherited


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("'env' must be a table of strings")
    out: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ValueError("'env' must map names to strings")
        out[name] = item
    return out


def _number(data: Mapping[str, object], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key!r} must be a number")
    return float(value)


@dataclass(frozen=True, slots=True)
class BridgeCheck:
    """What ``voice check`` can say about the bridge before anything uses it.

    Produced by :func:`check_bridge`, which never raises. That is the point of
    it: the bridge is optional, so its absence has to be answerable as a fact
    rather than as an exception on a start-up path that has nothing to do with
    voice.
    """

    configured: bool
    available: bool
    command: str
    detail: str


def check_bridge(config: BridgeConfig | None) -> BridgeCheck:
    """Say whether the bridge could be launched, without launching it.

    Named at check time and not at first use, because "first use" is the middle
    of a sentence the user is waiting on. Never raises: a missing bridge is a
    report, and a report is what lets ``pz-agent start`` carry on with the rest
    of the sidecar and simply not offer voice.
    """
    if config is None:
        return BridgeCheck(
            configured=False,
            available=False,
            command="",
            detail="the voice bridge is not configured; voice runs without it",
        )
    program = config.command[0]
    resolved = which(program)
    if resolved is None and Path(program).is_file():
        # `which` only answers for names on PATH and for paths it considers
        # executable; an absolute path to a script the user launches through an
        # interpreter is still a file that exists, and saying otherwise would
        # send them looking for the wrong problem.
        resolved = program
    if resolved is None:
        return BridgeCheck(
            configured=True,
            available=False,
            command=program,
            detail=f"the voice bridge program {program!r} was not found on PATH",
        )
    return BridgeCheck(
        configured=True,
        available=True,
        command=resolved,
        detail=f"the voice bridge program is {resolved}",
    )


@dataclass(frozen=True, slots=True)
class _Down:
    """The reader saw the far end end. Wakes whoever is waiting for a reply."""

    reason: BridgeReason
    detail: str
    fatal: bool


_Event: TypeAlias = "BridgeMessage | _Down"


class JsonlBridge:
    """Supervises one bridge process and the JSONL conversation with it.

    Synchronous on purpose. Everything below it is blocking — a pipe, a process,
    a thread — and wrapping blocking calls in coroutines does not make them
    interruptible, it only makes the blocking harder to see.
    :class:`TeamONBridgeClient` is the async face, and it is a thin one.
    """

    def __init__(self, config: BridgeConfig, *, listener: BridgeListener | None = None) -> None:
        self._config = config
        self._listener = listener
        self._state = BridgeState.STOPPED
        self._process: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=config.max_pending)
        self._events: queue.Queue[_Event] = queue.Queue(maxsize=config.max_pending)
        self._transcripts: queue.Queue[BridgeTranscript] = queue.Queue(maxsize=config.max_pending)
        self._restarts = 0
        self._generation = 0
        self._death = ""

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def restarts(self) -> int:
        """How many times this bridge has been relaunched after a crash."""
        return self._restarts

    @property
    def alive(self) -> bool:
        """Whether a child process exists and has not exited."""
        process = self._process
        return process is not None and process.poll() is None

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Launch the bridge and complete the handshake.

        Raises:
            BridgeUnavailable: the program is not there, it died during the
                handshake, or it never answered within ``startup_timeout``.
            ProtocolMismatch: it answered in a MAJOR version this build cannot
                read, which no amount of restarting fixes.
        """
        if self._state is BridgeState.DEAD:
            raise BridgeUnavailable(self._death)
        if self._state is BridgeState.RUNNING and self.alive:
            return
        self._launch()

    def stop(self) -> None:
        """Stop the bridge. Bounded by twice ``stop_timeout``, whatever it does.

        Safe to call twice, from any thread, and on a bridge that never started.
        Nothing here waits for the child to cooperate: it is asked once, killed
        once, and every join and every wait carries a timeout.
        """
        process, self._process = self._process, None
        self._generation += 1
        if self._state is not BridgeState.DEAD:
            self._state = BridgeState.STOPPED
        threads, self._threads = self._threads, []
        self._drain()
        if process is not None:
            self._terminate(process)
        for thread in threads:
            # Bounded, and its result is not checked: these are daemon threads
            # blocked on a pipe that has just been closed, and a thread that has
            # not noticed yet must not hold up the caller's shutdown.
            thread.join(timeout=self._config.stop_timeout)

    # -- sending ----------------------------------------------------------

    def send(self, message: BridgeMessage) -> None:
        """Queue *message* for the bridge. Never waits on the pipe.

        Raises:
            MessageTooLarge: the message is past the wire cap, so it is refused
                rather than truncated into something that decodes differently.
            BridgeUnavailable: the bridge is gone, or the outbox is full, which
                means it has stopped reading its stdin.
        """
        data = encode(message)
        self._ensure_running()
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            detail = (
                f"the voice bridge has not read {self._config.max_pending} queued messages; "
                f"a {message.type} message was dropped"
            )
            self._report(
                BridgeReport(state=self._state, reason=BridgeReason.DROPPED, detail=detail)
            )
            raise BridgeUnavailable(detail) from None

    def exchange(
        self,
        message: BridgeMessage,
        *,
        request_id: str,
        timeout: float,
    ) -> BridgeOutcome:
        """Send *message* and wait for the outcome that names *request_id*.

        Raises:
            BridgeTimeout: nothing came back in time. A silent bridge is a
                failure, not a wait — the companion has a user in front of it.
            BridgeFailed: the bridge reported a fault instead.
            BridgeUnavailable: it went away mid-exchange and cannot be replaced.
        """
        self.send(message)
        deadline = _deadline(timeout)
        while True:
            event = self._take_event(deadline, request_id)
            if isinstance(event, _Down):
                self._handle_down(event)
            elif event.type is BridgeMessageType.ERROR:
                fault = read_error(event)
                self._report(
                    BridgeReport(
                        state=self._state,
                        reason=BridgeReason.FAULT,
                        detail=f"the voice bridge reported a {fault.code} fault",
                        fault=fault,
                    )
                )
                raise BridgeFailed(fault)
            elif event.type is BridgeMessageType.OUTCOME:
                outcome = read_outcome(event)
                if outcome.request_id == request_id:
                    return outcome
                # An outcome for something else: a reply to a request that
                # already timed out. Dropping it is right, and the loop is still
                # bounded by the deadline.

    def next_transcript(self, timeout: float) -> BridgeTranscript | None:
        """The next recognition result, or ``None`` if none arrived in time.

        ``None`` is not an error: silence is the normal state of a microphone.
        """
        self._ensure_running()
        try:
            return self._transcripts.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    # -- internals --------------------------------------------------------

    def _launch(self) -> None:
        self._state = BridgeState.STARTING
        self._drain()
        self._generation += 1
        generation = self._generation
        try:
            process = subprocess.Popen(  # noqa: S603 - argv from a validated config, never a shell
                list(self._config.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # A pipe nobody drains is a pipe that fills and wedges the child.
                # The bridge reports through `error` messages, which are typed
                # and bounded; its diagnostics are its own business.
                stderr=subprocess.DEVNULL,
                env=self._config.child_environment(),
                bufsize=0,
                close_fds=True,
            )
        except OSError as exc:
            detail = (
                f"the voice bridge program {self._config.command[0]!r} could not be launched "
                f"({type(exc).__name__})"
            )
            self._state = BridgeState.STOPPED
            self._report(
                BridgeReport(
                    state=BridgeState.STOPPED, reason=BridgeReason.LAUNCH_FAILED, detail=detail
                )
            )
            raise BridgeUnavailable(detail) from exc
        self._process = process
        self._spawn_threads(process, generation)
        try:
            self.send(hello_message())
            ready = self._await_ready()
        except BridgeError:
            self._terminate(process)
            self._process = None
            raise
        read_ready(ready)
        self._state = BridgeState.RUNNING

    def _spawn_threads(self, process: subprocess.Popen[bytes], generation: int) -> None:
        outbox = self._outbox
        reader = threading.Thread(
            target=self._read_loop,
            args=(process, generation),
            name="pz-voice-bridge-reader",
            daemon=True,
        )
        writer = threading.Thread(
            target=self._write_loop,
            args=(process, outbox),
            name="pz-voice-bridge-writer",
            daemon=True,
        )
        self._threads = [reader, writer]
        reader.start()
        writer.start()

    def _await_ready(self) -> BridgeMessage:
        deadline = _deadline(self._config.startup_timeout)
        while True:
            event = self._take_event(deadline, "hello")
            if isinstance(event, _Down):
                self._handle_down(event)
            elif event.type is BridgeMessageType.READY:
                return event
            elif event.type is BridgeMessageType.ERROR:
                fault = read_error(event)
                self._report(
                    BridgeReport(
                        state=BridgeState.STARTING,
                        reason=BridgeReason.HANDSHAKE_FAILED,
                        detail=f"the voice bridge reported a {fault.code} fault while starting",
                        fault=fault,
                    )
                )
                raise BridgeFailed(fault)

    def _take_event(self, deadline: float, request_id: str) -> _Event:
        remaining = deadline - _now()
        if remaining <= 0:
            raise BridgeTimeout(f"the voice bridge did not answer {request_id!r} in time")
        try:
            return self._events.get(timeout=remaining)
        except queue.Empty:
            raise BridgeTimeout(f"the voice bridge did not answer {request_id!r} in time") from None

    def _handle_down(self, down: _Down) -> None:
        if down.fatal:
            self._die(down.reason, down.detail)
            if down.reason is BridgeReason.VERSION_MISMATCH:
                raise ProtocolMismatch(down.detail)
            raise BridgeUnavailable(down.detail)
        raise BridgeUnavailable(down.detail)

    def _ensure_running(self) -> None:
        if self._state is BridgeState.DEAD:
            raise BridgeUnavailable(self._death)
        if self._state is BridgeState.STOPPED:
            raise BridgeUnavailable("the voice bridge is not running")
        if self.alive:
            return
        self._restart()

    def _restart(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            self._terminate(process)
        if self._restarts >= self._config.max_restarts:
            detail = (
                f"the voice bridge has been restarted {self._restarts} times, which is the "
                "limit; voice is off until it is fixed and the sidecar restarted"
            )
            self._die(BridgeReason.RESTART_LIMIT, detail)
            raise BridgeUnavailable(detail)
        self._restarts += 1
        self._report(
            BridgeReport(
                state=BridgeState.STARTING,
                reason=BridgeReason.RESTARTED,
                detail=(
                    f"the voice bridge stopped; restarting it, attempt {self._restarts} "
                    f"of {self._config.max_restarts}"
                ),
            )
        )
        self._launch()

    def _die(self, reason: BridgeReason, detail: str) -> None:
        self._death = detail
        self._state = BridgeState.DEAD
        process, self._process = self._process, None
        if process is not None:
            self._terminate(process)
        self._report(BridgeReport(state=BridgeState.DEAD, reason=reason, detail=detail))

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """End *process*, bounded by twice ``stop_timeout``, then let it go."""
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=self._config.stop_timeout)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    process.kill()
                # If this one expires too the child is unkillable — an
                # uninterruptible syscall, or a debugger. Reporting a stop that
                # did not finish is better than a stop that never returns.
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=self._config.stop_timeout)
        # After the child is gone, so no thread is parked on them.
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()

    def _drain(self) -> None:
        """Forget everything queued for a process that is no longer there."""
        outbox = self._outbox
        for pending in (outbox, self._events, self._transcripts):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
        # Wakes a writer thread parked on `get`; the queue was just emptied, so
        # this cannot itself block.
        with suppress(queue.Full):
            outbox.put_nowait(None)
        self._outbox = queue.Queue(maxsize=self._config.max_pending)

    def _report(self, report: BridgeReport) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            listener(report)
        except Exception:
            # A listener that raises must not take the reader thread with it:
            # losing one log line is smaller than losing every transcript after
            # it, and this is the path a *failing* bridge reports through.
            return

    # -- the two threads --------------------------------------------------

    def _write_loop(
        self, process: subprocess.Popen[bytes], outbox: queue.Queue[bytes | None]
    ) -> None:
        stdin = process.stdin
        if stdin is None:
            return
        while True:
            item = outbox.get()
            if item is None:
                return
            try:
                stdin.write(item)
                stdin.flush()
            except (OSError, ValueError):
                # The pipe is gone: the bridge exited, or the stop path closed
                # it. Either way the reader is the one that reports it, and a
                # second report of the same event would only be noise.
                return

    def _read_loop(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        framer = LineFramer()
        while True:
            try:
                chunk = stdout.read(_CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            for item in framer.feed(chunk):
                if self._generation != generation:
                    return
                self._admit(item, generation)
        if self._generation == generation:
            self._offer_event(
                _Down(
                    reason=BridgeReason.CRASHED,
                    detail="the voice bridge closed its output; it has stopped",
                    fatal=False,
                ),
                generation,
            )

    def _admit(self, item: bytes | OverlongLine, generation: int) -> None:
        if isinstance(item, OverlongLine):
            self._report(
                BridgeReport(
                    state=self._state,
                    reason=BridgeReason.OVERLONG_LINE,
                    detail=(
                        f"the voice bridge sent a line of at least {item.dropped_bytes} bytes; "
                        "it was dropped without being buffered"
                    ),
                )
            )
            return
        if not item.strip():
            return
        try:
            message = decode(item)
        except ProtocolMismatch as exc:
            self._offer_event(
                _Down(reason=BridgeReason.VERSION_MISMATCH, detail=str(exc), fatal=True),
                generation,
            )
            self._report(
                BridgeReport(
                    state=self._state, reason=BridgeReason.VERSION_MISMATCH, detail=str(exc)
                )
            )
            return
        except BridgeProtocolError as exc:
            reason = (
                BridgeReason.UNKNOWN_TYPE
                if isinstance(exc, UnknownMessageType)
                else BridgeReason.MALFORMED_LINE
            )
            self._report(
                BridgeReport(
                    state=self._state,
                    reason=reason,
                    detail=f"a line from the voice bridge was refused: {exc}",
                )
            )
            return
        if message.type is BridgeMessageType.TRANSCRIPT:
            self._offer_transcript(message, generation)
            return
        self._offer_event(message, generation)

    def _offer_transcript(self, message: BridgeMessage, generation: int) -> None:
        try:
            transcript = read_transcript(message)
        except BridgeProtocolError as exc:
            self._report(
                BridgeReport(
                    state=self._state,
                    reason=BridgeReason.MALFORMED_LINE,
                    detail=f"a transcript from the voice bridge was refused: {exc}",
                )
            )
            return
        if self._generation != generation:
            return
        try:
            self._transcripts.put_nowait(transcript)
        except queue.Full:
            # Drop the oldest: a transcript is only useful while it is fresh,
            # and the newest one is the one the user is waiting on.
            with suppress(queue.Empty):
                self._transcripts.get_nowait()
            with suppress(queue.Full):
                self._transcripts.put_nowait(transcript)
            self._report(
                BridgeReport(
                    state=self._state,
                    reason=BridgeReason.DROPPED,
                    detail="the transcript queue was full; the oldest transcript was dropped",
                )
            )

    def _offer_event(self, event: _Event, generation: int) -> None:
        if self._generation != generation:
            return
        try:
            self._events.put_nowait(event)
        except queue.Full:
            with suppress(queue.Empty):
                self._events.get_nowait()
            with suppress(queue.Full):
                self._events.put_nowait(event)
            self._report(
                BridgeReport(
                    state=self._state,
                    reason=BridgeReason.DROPPED,
                    detail="the reply queue was full; the oldest reply was dropped",
                )
            )


def _now() -> float:
    return time.monotonic()


def _deadline(timeout: float) -> float:
    return _now() + min(timeout, MAX_TIMEOUT_SECONDS)


class TeamONBridgeClient:
    """A :class:`~pz_agent_voice.adapters.teamon.TeamONClient` over the bridge.

    The three methods the adapter needs, each mapped onto one message type, and
    each running its blocking half in a worker thread so the companion's event
    loop keeps turning while a sentence is being spoken. Beyond them,
    :meth:`submit_goal` carries a goal token across and waits for the outcome,
    which is how the companion learns that the goal *ended* rather than assuming
    it did.
    """

    def __init__(self, config: BridgeConfig, *, listener: BridgeListener | None = None) -> None:
        self._bridge = JsonlBridge(config, listener=listener)
        self._config = config

    @property
    def bridge(self) -> JsonlBridge:
        return self._bridge

    @property
    def state(self) -> BridgeState:
        return self._bridge.state

    async def start(self) -> None:
        """Launch the bridge and complete the handshake."""
        await asyncio.to_thread(self._bridge.start)

    async def transcripts(self) -> AsyncIterator[TeamONTranscript]:
        """Open the recognition stream, starting the bridge if it is not up."""
        await self.start()
        return self._stream()

    async def _stream(self) -> AsyncIterator[TeamONTranscript]:
        while True:
            try:
                item = await asyncio.to_thread(
                    self._bridge.next_transcript, _TRANSCRIPT_POLL_SECONDS
                )
            except BridgeUnavailable:
                # The bridge is gone and has already been reported — with a
                # sentence for the user, if it died rather than being stopped.
                # Ending the iterator ends the session, which is the contract.
                return
            if item is None:
                continue
            yield TeamONTranscript(
                text=item.text,
                at_ms=item.at_ms,
                final=item.final,
                confidence=item.confidence,
            )

    async def synthesize(self, request: TeamONSpeech) -> None:
        """Speak *request*, returning when it was delivered or abandoned.

        Raises:
            BridgeFailed: the bridge could not say it. Distinct from a
                cancellation, which returns normally — the adapter's own
                cancellation counter is what turns that into ``SpeechCancelled``.
            BridgeTimeout: nothing came back within ``speech_timeout``.
        """
        message = speak_message(
            utterance_id=request.utterance_id,
            text=request.text,
            priority=request.priority,
            interruptible=request.interruptible,
        )
        outcome = await asyncio.to_thread(
            self._exchange, message, request.utterance_id, self._config.speech_timeout
        )
        if outcome.status in {OutcomeStatus.FAILED, OutcomeStatus.REFUSED}:
            raise BridgeFailed(_SYNTHESIS_FAULT)

    async def interrupt(self, utterance_id: str) -> None:
        """Abandon *utterance_id* now, and never fail because of it.

        A wedged bridge is precisely when an interrupt cannot be delivered, and
        it is also when the user is most likely to be saying «стоп». Raising
        here would put a transport failure on the one path that must not have
        one; the utterance is abandoned on this side either way.
        """
        try:
            await asyncio.to_thread(self._bridge.send, interrupt_message(utterance_id))
        except BridgeError:
            return

    async def submit_goal(self, *, request_id: str, goal: VoiceGoal) -> BridgeOutcome:
        """Dispatch *goal* and wait for the bridge to say how it ended."""
        message = goal_message(request_id=request_id, goal=goal)
        return await asyncio.to_thread(
            self._exchange, message, request_id, self._config.reply_timeout
        )

    async def aclose(self) -> None:
        """Stop the bridge. Bounded, like :meth:`JsonlBridge.stop`."""
        await asyncio.to_thread(self._bridge.stop)

    def _exchange(self, message: BridgeMessage, request_id: str, timeout: float) -> BridgeOutcome:
        return self._bridge.exchange(message, request_id=request_id, timeout=timeout)
