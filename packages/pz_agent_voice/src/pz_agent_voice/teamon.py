"""The TeamON bridge: a child process, two pipes, and everything bounded.

:class:`TeamONBridgeClient` is the concrete
:class:`~pz_agent_voice.adapters.teamon.TeamONClient` this build ships. The
vendor SDK is not installed here and its surface is unverified, so the
integration point is a *process boundary*: a small program — the bridge — links
against the SDK, speaks the JSONL protocol defined below on its stdin and
stdout, and this module supervises it.

That boundary is the design, not a workaround. A vendor SDK loaded in-process
gets the sidecar's memory, its exception handling and its exit code; a vendor
SDK behind a pipe gets a fixed byte budget and a kill switch. Everything here
follows from it.

**One JSON object per line, and a line is bounded before it is parsed.**
:func:`encode` writes through ``json.dumps``, which escapes every control
character, so a newline *inside* a field is ``\\n`` on the wire and a message can
never span two lines. :class:`LineFramer` splits the other direction and drops a
line past :data:`MAX_LINE_BYTES` *as the bytes arrive* rather than accumulating
it: a bridge that emits a gigabyte without a newline costs this process a fixed
buffer and a counter. The line after the dropped one still parses, and a line
that is not JSON is reported and skipped — one malformed message must not end a
session the user is speaking into.

**Two threads, never the caller's.** A reader drains stdout into bounded queues
and a writer drains a bounded outbox into stdin. A caller that wants to send
something puts it in a queue and returns, so a bridge that has stopped reading
its stdin cannot wedge the companion: the pipe fills, the writer thread blocks,
and :meth:`JsonlBridge.send` refuses instead of growing.

**Every wait a caller can reach has a deadline.** Every ``get`` a caller's
thread performs carries the remaining time to an explicit deadline, every
``wait`` on the child carries a timeout, and every thread join carries one. A
silent bridge produces :class:`BridgeTimeout`, never a hang. The one wait
without a deadline is the writer thread's ``get`` on the outbox, which is
deliberate and is not on any caller's path: it is a daemon thread parked on a
queue, woken either by work or by the sentinel :meth:`JsonlBridge._drain` puts
there, and a timeout on it would only make it spin.

**The stop path never waits for agreement.** :meth:`JsonlBridge.stop` signals,
waits ``stop_timeout``, kills, waits ``stop_timeout`` again and gives up — at
most twice a bound the caller set, whatever the bridge does. Nothing about the
bridge can therefore delay a panic stop: the interrupt is a queue put that
refuses when full, and the stop is bounded even against a child that ignores
its termination signal.

**Restarts are counted.** A crashed bridge is relaunched on the next use, up to
``max_restarts``; past that the bridge is dead, the state says so, and a report
goes to the listener carrying a sentence the user hears. A dead bridge that
reported nothing would leave someone talking to a machine that stopped listening
several minutes ago.

**No credential lives here.** :class:`BridgeConfig` refuses a field whose *name*
looks like a secret — in the mapping it is loaded from, in the environment it
passes on, and in the option-shaped arguments it launches — and the child's
environment is an allowlist, so the parent's own secrets do not travel either.
Whatever the vendor SDK needs to authenticate, the bridge program reads for
itself, from somewhere this process never touches.

**Refusals name the failure and quote nothing.** No message raised or reported
from this module contains a filesystem path, a transcript, a configuration value
or any string the far end chose. Type names, status names and version strings
arriving on the pipe are attacker-controlled, so they are described rather than
echoed; the version is the one exception and only after it has been parsed into
two integers by this module's own regex. These strings reach tracebacks and bug
reports long before any redactor sees them.

**Windows is the shipping platform.** Two shapes are constructed explicitly so
they are visible on a Linux test machine: :func:`creation_flags` returns
``CREATE_NO_WINDOW`` for a ``win32`` platform string, so the bridge does not
flash a console window over a full-screen game, and :func:`program_is_path`
understands the backslash separator and the drive-relative form ``C:bridge.exe``
that holds no separator at all. The framer strips the ``\\r`` that a child
writing text-mode stdout on Windows puts before every newline.
"""

from __future__ import annotations

# The sentences in FAULT_PHRASES and BRIDGE_UNAVAILABLE_PHRASE are spoken
# Russian, short enough that every letter in them has a Latin lookalike. Taking
# the confusable-character rule's suggestion would leave a synthesiser reading
# out a word that is not a word.
# ruff: noqa: RUF001
import asyncio
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from shutil import which
from types import MappingProxyType
from typing import Final, TypeAlias

from .adapters.teamon import TeamONSpeech, TeamONTranscript
from .messages import MAX_TEXT_CHARS, MAX_TRANSCRIPT_CHARS, VoiceGoal

__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "BRIDGE_UNAVAILABLE_PHRASE",
    "CREATE_NO_WINDOW",
    "FAULT_PHRASES",
    "MAX_LINE_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "MESSAGE_DIRECTIONS",
    "BridgeCheck",
    "BridgeConfig",
    "BridgeDirection",
    "BridgeError",
    "BridgeFailed",
    "BridgeFault",
    "BridgeFaultCode",
    "BridgeListener",
    "BridgeMessage",
    "BridgeMessageType",
    "BridgeOutcome",
    "BridgeProtocolError",
    "BridgeReason",
    "BridgeReport",
    "BridgeState",
    "BridgeTimeout",
    "BridgeTranscript",
    "BridgeUnavailable",
    "JsonlBridge",
    "LineFramer",
    "MalformedMessage",
    "MessageTooLarge",
    "OutcomeStatus",
    "OverlongLine",
    "ProtocolMismatch",
    "TeamONBridgeClient",
    "UnknownMessageType",
    "check_bridge",
    "check_protocol_version",
    "creation_flags",
    "decode",
    "encode",
    "goal_message",
    "hello_message",
    "interrupt_message",
    "parse_version",
    "program_is_path",
    "read_error",
    "read_outcome",
    "read_ready",
    "read_transcript",
    "speak_message",
]


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------

#: MAJOR.MINOR. MAJOR changes when a field changes meaning or disappears; MINOR
#: changes when one is added. Both sides send it on every message, so a mismatch
#: is caught on the first line rather than on the first field that differs.
BRIDGE_PROTOCOL_VERSION: Final = "1.0"

#: The largest message this side will send. Every legal message is a handful of
#: short fields — the longest is a ``speak`` carrying at most
#: :data:`~pz_agent_voice.messages.MAX_TEXT_CHARS` characters — so this is two
#: orders of magnitude of headroom and still a fixed number of bytes.
MAX_MESSAGE_BYTES: Final = 16_384

#: The longest line the reader will assemble. Equal to the send cap on purpose:
#: a line that cannot hold a legal message is not worth buffering, so the reader
#: drops it the moment it passes the cap instead of waiting for a newline that
#: may never come.
MAX_LINE_BYTES: Final = MAX_MESSAGE_BYTES

#: Ceiling on every configurable wait. A timeout is a bound; a bound of an hour
#: is a bound in name only, and the companion is a real-time thing — nothing it
#: does is worth a minute of silence.
MAX_TIMEOUT_SECONDS: Final = 60.0

#: Correlation handles are minted by this side (``teamon-3``) and are shape
#: checked rather than sanitised, so a handle can never carry punctuation into a
#: JSON field or into a log line.
_HANDLE: Final = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")

_VERSION: Final = re.compile(r"^(\d{1,3})\.(\d{1,4})$")


class BridgeMessageType(StrEnum):
    """Every message this protocol has. Anything else is refused.

    To the bridge: ``hello`` opens the session and declares this build's
    protocol version; ``speak`` carries one utterance to synthesise;
    ``interrupt`` abandons one utterance now; ``goal`` dispatches one member of
    :class:`~pz_agent_voice.messages.VoiceGoal` under a request id — a token,
    never transcript text, because the channel from a microphone to anything
    else is that enum and nothing wider.

    From the bridge: ``ready`` answers ``hello``; ``transcript`` is one
    recognition result, interim hypotheses included, because the stop word
    depends on them; ``outcome`` says how a ``speak`` or a ``goal`` ended;
    ``error`` reports a bridge-side failure.
    """

    HELLO = "hello"
    SPEAK = "speak"
    INTERRUPT = "interrupt"
    GOAL = "goal"
    READY = "ready"
    TRANSCRIPT = "transcript"
    OUTCOME = "outcome"
    ERROR = "error"


class BridgeDirection(StrEnum):
    """Which way a message is allowed to travel."""

    TO_BRIDGE = "to_bridge"
    FROM_BRIDGE = "from_bridge"


MESSAGE_DIRECTIONS: Final[Mapping[BridgeMessageType, BridgeDirection]] = MappingProxyType(
    {
        BridgeMessageType.HELLO: BridgeDirection.TO_BRIDGE,
        BridgeMessageType.SPEAK: BridgeDirection.TO_BRIDGE,
        BridgeMessageType.INTERRUPT: BridgeDirection.TO_BRIDGE,
        BridgeMessageType.GOAL: BridgeDirection.TO_BRIDGE,
        BridgeMessageType.READY: BridgeDirection.FROM_BRIDGE,
        BridgeMessageType.TRANSCRIPT: BridgeDirection.FROM_BRIDGE,
        BridgeMessageType.OUTCOME: BridgeDirection.FROM_BRIDGE,
        BridgeMessageType.ERROR: BridgeDirection.FROM_BRIDGE,
    }
)

if set(MESSAGE_DIRECTIONS) != set(BridgeMessageType):
    raise RuntimeError("every message type must declare the direction it travels in")


class BridgeProtocolError(ValueError):
    """A line, or a message, that this protocol will not accept."""


class MalformedMessage(BridgeProtocolError):
    """Not UTF-8, not JSON, not an object, or missing a field it must carry."""


class UnknownMessageType(BridgeProtocolError):
    """A type this build does not have, or one travelling the wrong way."""


class MessageTooLarge(BridgeProtocolError):
    """Past the byte cap, in either direction."""


class ProtocolMismatch(BridgeProtocolError):
    """The two sides disagree about MAJOR, so no field name is safe to read."""


class OutcomeStatus(StrEnum):
    """How a ``speak`` or a ``goal`` ended.

    ``ENDED`` is the only member that claims the thing was carried out. The
    other three are all "it stopped", distinguished because the companion says
    something different about each, and because a refusal is not a failure.
    """

    ENDED = "ended"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"


class BridgeFaultCode(StrEnum):
    """The closed set of things the bridge is allowed to say went wrong."""

    AUDIO_DEVICE = "audio_device"
    RECOGNISER = "recogniser"
    SYNTHESIS = "synthesis"
    NETWORK = "network"
    INTERNAL = "internal"
    #: What any code this build does not know becomes. An error is already the
    #: failure path, and refusing the report of a failure loses the failure.
    UNKNOWN = "unknown"


FAULT_PHRASES: Final[Mapping[BridgeFaultCode, str]] = MappingProxyType(
    {
        BridgeFaultCode.AUDIO_DEVICE: "Микрофон недоступен.",
        BridgeFaultCode.RECOGNISER: "Распознавание не работает.",
        BridgeFaultCode.SYNTHESIS: "Не могу говорить.",
        BridgeFaultCode.NETWORK: "Голосовой сервис не отвечает.",
        BridgeFaultCode.INTERNAL: "Ошибка голосового моста.",
        BridgeFaultCode.UNKNOWN: "Ошибка голосового моста.",
    }
)

if set(FAULT_PHRASES) != set(BridgeFaultCode):
    raise RuntimeError("every fault code must have a sentence the companion can say")

#: Said once when the bridge is gone for good. It names the component so the
#: user knows which half stopped working, and nothing else: what the bridge
#: wrote about its own death is a string from another process.
BRIDGE_UNAVAILABLE_PHRASE: Final = "Голосовой мост не работает."


@dataclass(frozen=True, slots=True)
class BridgeMessage:
    """One decoded message: its type, and the fields the type declares."""

    type: BridgeMessageType
    fields: Mapping[str, object]

    def direction(self) -> BridgeDirection:
        return MESSAGE_DIRECTIONS[self.type]


@dataclass(frozen=True, slots=True)
class BridgeTranscript:
    """One recognition result, bounded and clamped on the way in."""

    text: str
    at_ms: int
    final: bool
    confidence: float


@dataclass(frozen=True, slots=True)
class BridgeOutcome:
    """What became of one ``speak`` or one ``goal``."""

    request_id: str
    status: OutcomeStatus

    @property
    def ended(self) -> bool:
        """True only when the bridge said the thing ran to its end."""
        return self.status is OutcomeStatus.ENDED


@dataclass(frozen=True, slots=True)
class BridgeFault:
    """A bridge-side failure, reduced to a code this build has a sentence for.

    The vendor's own words are not a field here. They cannot be: this object is
    what the companion says out loud, and reading an arbitrary string from
    another process through a text-to-speech engine is how a stack trace, a file
    path or somebody's key ends up spoken into a microphone-shaped room.
    """

    code: BridgeFaultCode

    def summary(self) -> str:
        """The one sentence this fault is allowed to become."""
        return FAULT_PHRASES[self.code]


@dataclass(frozen=True, slots=True)
class OverlongLine:
    """A line that passed the cap. Its bytes were counted and then dropped."""

    dropped_bytes: int


def parse_version(raw: str) -> tuple[int, int]:
    """``"1.0"`` → ``(1, 0)``.

    Raises:
        MalformedMessage: it is not two small integers separated by a dot. The
            string itself is not quoted back — it came off the pipe.
    """
    match = _VERSION.match(raw)
    if match is None:
        raise MalformedMessage(
            "the protocol version must look like MAJOR.MINOR, and this one does not; "
            "install a bridge that speaks this build's protocol"
        )
    return int(match.group(1)), int(match.group(2))


def check_protocol_version(raw: str) -> tuple[int, int]:
    """Accept *raw* if its MAJOR matches this build's.

    The reported version is quoted back only after this module's own regex has
    reduced it to two integers, so what reaches the message is at most seven
    digits of this module's own formatting and never the far end's bytes.

    Raises:
        MalformedMessage: the version is not MAJOR.MINOR.
        ProtocolMismatch: the MAJOR differs, so the field names on the wire no
            longer mean what this build thinks they mean.
    """
    reported = parse_version(raw)
    ours = parse_version(BRIDGE_PROTOCOL_VERSION)
    if reported[0] != ours[0]:
        raise ProtocolMismatch(
            f"the bridge speaks protocol {reported[0]}.{reported[1]} and this build speaks "
            f"{BRIDGE_PROTOCOL_VERSION}; install a bridge built for {ours[0]}.x"
        )
    return reported


def encode(message: BridgeMessage) -> bytes:
    """One message as one line, newline included.

    ``json.dumps`` escapes control characters, so a field containing a newline
    is written as ``\\n`` and the line still ends exactly once, at its end.

    Raises:
        MessageTooLarge: the encoded form is past :data:`MAX_MESSAGE_BYTES`.
        MalformedMessage: a field is not JSON-representable.
    """
    body = {"v": BRIDGE_PROTOCOL_VERSION, "type": str(message.type), **message.fields}
    try:
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MalformedMessage(
            f"a {message.type} message carries a field that cannot be written as JSON "
            f"({type(exc).__name__})"
        ) from exc
    data = text.encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge(
            f"a {message.type} message is {len(data)} bytes and the limit is {MAX_MESSAGE_BYTES}"
        )
    return data + b"\n"


def decode(line: bytes, *, expect: BridgeDirection = BridgeDirection.FROM_BRIDGE) -> BridgeMessage:
    """One line as one message, refusing everything this build does not know.

    Args:
        line: the line's bytes, without its newline.
        expect: which way this line is supposed to be travelling. A ``speak``
            arriving *from* the bridge is as wrong as a type that does not
            exist, and for the same reason: something on the far end is not the
            bridge this build talks to.

    Raises:
        MessageTooLarge: the line is past :data:`MAX_LINE_BYTES`.
        MalformedMessage: not UTF-8, not JSON, not an object, or no version.
        UnknownMessageType: a type outside :class:`BridgeMessageType`, or one
            declared to travel the other way.
        ProtocolMismatch: a MAJOR version this build cannot read.
    """
    if len(line) > MAX_LINE_BYTES:
        raise MessageTooLarge(f"a line of {len(line)} bytes is past the {MAX_LINE_BYTES} byte cap")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedMessage("a line that is not UTF-8 is not a message") from exc
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        # `exc.colno` is a position, not content. The line is never quoted: the
        # far end chooses what is in it and this string reaches a log.
        raise MalformedMessage(f"a line that is not JSON, at column {exc.colno}") from exc
    if not isinstance(body, dict):
        raise MalformedMessage(f"a message must be a JSON object, got {type(body).__name__}")
    kind = _message_type(body.get("type"), expect)
    version = body.get("v")
    if not isinstance(version, str):
        raise MalformedMessage(f"a {kind} message must carry the protocol version in 'v'")
    check_protocol_version(version)
    return BridgeMessage(type=kind, fields=dict(body))


def _known_types() -> str:
    return ", ".join(sorted(member.value for member in BridgeMessageType))


def _message_type(raw: object, expect: BridgeDirection) -> BridgeMessageType:
    if not isinstance(raw, str):
        raise MalformedMessage("a message must carry a string 'type'")
    try:
        kind = BridgeMessageType(raw)
    except ValueError as exc:
        # The name is not echoed. It is a string the far end chose, and this
        # message goes to a log and from there into a bug report.
        raise UnknownMessageType(
            f"a message names a type this build does not have; known types are {_known_types()}"
        ) from exc
    if MESSAGE_DIRECTIONS[kind] is not expect:
        raise UnknownMessageType(
            f"a {kind} message travels {MESSAGE_DIRECTIONS[kind]}, and this line arrived "
            f"as {expect}"
        )
    return kind


# -- what this side sends ---------------------------------------------------


def hello_message() -> BridgeMessage:
    """Open the session, declaring this build's protocol version."""
    return BridgeMessage(type=BridgeMessageType.HELLO, fields={})


def speak_message(
    *,
    utterance_id: str,
    text: str,
    priority: int,
    interruptible: bool,
) -> BridgeMessage:
    """One utterance to synthesise.

    Raises:
        MalformedMessage: the handle is not a handle, the text is empty or past
            :data:`~pz_agent_voice.messages.MAX_TEXT_CHARS`, or the priority is
            negative. Refused rather than truncated: an utterance the queue
            already bounded and this call still finds too long is a bug on this
            side, and truncating would hide it mid-sentence.
    """
    _check_handle(utterance_id, "utterance_id")
    if not text.strip():
        raise MalformedMessage("an utterance must carry something to say")
    if len(text) > MAX_TEXT_CHARS:
        raise MalformedMessage(
            f"an utterance may be at most {MAX_TEXT_CHARS} characters, got {len(text)}"
        )
    if priority < 0:
        raise MalformedMessage(f"priority must be non-negative, got {priority}")
    return BridgeMessage(
        type=BridgeMessageType.SPEAK,
        fields={
            "utterance_id": utterance_id,
            "text": text,
            "priority": priority,
            "interruptible": interruptible,
        },
    )


def interrupt_message(utterance_id: str) -> BridgeMessage:
    """Abandon *utterance_id* now."""
    _check_handle(utterance_id, "utterance_id")
    return BridgeMessage(type=BridgeMessageType.INTERRUPT, fields={"utterance_id": utterance_id})


def goal_message(*, request_id: str, goal: VoiceGoal) -> BridgeMessage:
    """Dispatch one goal token under *request_id*.

    *goal* is a :class:`~pz_agent_voice.messages.VoiceGoal` rather than a string
    so that the widest thing this process can put on the pipe is one of four
    enum members. There is no branch here that forwards a transcript.
    """
    _check_handle(request_id, "request_id")
    return BridgeMessage(
        type=BridgeMessageType.GOAL,
        fields={"request_id": request_id, "goal": str(goal)},
    )


def _check_handle(value: str, field_name: str) -> None:
    if not _HANDLE.match(value):
        # The handle is minted by this side, so the failure is a bug here and
        # the *rule* is what the reader needs. The value stays out of it: a
        # caller could have built it from something it should not have.
        raise MalformedMessage(
            f"{field_name} must be a lowercase handle of 1 to 64 characters drawn from "
            "letters, digits, dot, dash and underscore"
        )


# -- what this side reads ---------------------------------------------------


def read_ready(message: BridgeMessage) -> tuple[int, int]:
    """The bridge's protocol version, already checked for MAJOR agreement."""
    _expect(message, BridgeMessageType.READY)
    version = message.fields.get("v")
    if not isinstance(version, str):
        raise MalformedMessage("a ready message must carry the protocol version in 'v'")
    return check_protocol_version(version)


def read_transcript(message: BridgeMessage) -> BridgeTranscript:
    """One recognition result, bounded and clamped.

    Bounded rather than refused because a recogniser that emits a paragraph of
    accumulated context is a recogniser doing something normal, and clamped
    because a client reporting a confidence of 1.7 would otherwise sail past the
    gate that exists to stop the companion acting on a guess.
    """
    _expect(message, BridgeMessageType.TRANSCRIPT)
    text = message.fields.get("text")
    if not isinstance(text, str):
        raise MalformedMessage("a transcript must carry 'text' as a string")
    at_ms = _read_int(message.fields.get("at_ms"), "at_ms")
    final = message.fields.get("final", True)
    if not isinstance(final, bool):
        raise MalformedMessage("a transcript's 'final' must be true or false")
    confidence = _read_float(message.fields.get("confidence", 1.0), "confidence")
    return BridgeTranscript(
        text=text[:MAX_TRANSCRIPT_CHARS],
        at_ms=max(0, at_ms),
        final=final,
        confidence=min(1.0, max(0.0, confidence)),
    )


def read_outcome(message: BridgeMessage) -> BridgeOutcome:
    """What became of the request the bridge names.

    An unrecognised status is refused rather than mapped to a failure: this is
    the message the companion uses to decide whether a goal *ended*, and a
    status it cannot read is a question, not an answer.
    """
    _expect(message, BridgeMessageType.OUTCOME)
    request_id = message.fields.get("request_id")
    if not isinstance(request_id, str) or not _HANDLE.match(request_id):
        raise MalformedMessage("an outcome must name the request it belongs to, as a handle")
    raw = message.fields.get("status")
    if not isinstance(raw, str):
        raise MalformedMessage("an outcome must carry 'status' as a string")
    try:
        status = OutcomeStatus(raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in OutcomeStatus))
        raise MalformedMessage(
            f"an outcome carries a status this build does not have; known ones are {known}"
        ) from exc
    return BridgeOutcome(request_id=request_id, status=status)


def read_error(message: BridgeMessage) -> BridgeFault:
    """Classify a bridge-side failure, and drop everything it wrote about it.

    ``detail`` is read here and goes no further — not into the returned object,
    not into a log line, not into the exception this becomes. It is the one
    field the far end fills with free text, and every consumer of a fault in
    this package eventually reaches a speech synthesiser.
    """
    _expect(message, BridgeMessageType.ERROR)
    raw = message.fields.get("code")
    if not isinstance(raw, str):
        return BridgeFault(code=BridgeFaultCode.UNKNOWN)
    try:
        return BridgeFault(code=BridgeFaultCode(raw))
    except ValueError:
        # A code from a newer bridge is still a failure, and reporting it as an
        # unknown failure loses less than refusing the only message that says
        # anything went wrong.
        return BridgeFault(code=BridgeFaultCode.UNKNOWN)


def _expect(message: BridgeMessage, kind: BridgeMessageType) -> None:
    if message.type is not kind:
        raise MalformedMessage(f"expected a {kind} message, got {message.type}")


def _read_int(value: object, field_name: str) -> int:
    # `bool` is an `int` in Python, and `True` as a timestamp is a bug that
    # would otherwise read as one millisecond.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedMessage(f"{field_name} must be a whole number")
    return value


def _read_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedMessage(f"{field_name} must be a number")
    return float(value)


class LineFramer:
    """Splits a byte stream into lines, dropping any line past the cap.

    The dropping is what makes this bounded, and it happens *as the bytes
    arrive*: once the current line has passed the cap the framer stops keeping
    any of it and skips forward to the next newline. A bridge emitting one
    enormous line therefore costs a fixed buffer and a counter, and the line
    after it still parses — which is the recovery the reader depends on, since
    the alternative is a session that ends because one message was malformed.

    A single trailing ``\\r`` is removed. That is the Windows shape: the bridge
    is a console program there, and a child writing its stdout in text mode
    turns every ``\\n`` into ``\\r\\n``. Exactly one is stripped, so a ``\\r``
    that a hostile far end packed in on purpose still makes the line fail to
    parse rather than silently vanishing.
    """

    __slots__ = ("_buffer", "_dropped", "_dropping", "_limit")

    def __init__(self, *, limit: int = MAX_LINE_BYTES) -> None:
        if limit < 1:
            raise ValueError(f"the line limit must be positive, got {limit}")
        self._limit = limit
        self._buffer = bytearray()
        self._dropping = False
        self._dropped = 0

    @property
    def buffered(self) -> int:
        """Bytes currently held for the line in progress. Never past the cap."""
        return len(self._buffer)

    @property
    def limit(self) -> int:
        return self._limit

    def feed(self, chunk: bytes) -> list[bytes | OverlongLine]:
        """Everything *chunk* completed: whole lines, and lines that were dropped.

        A list rather than an iterator: a generator the caller forgot to drain
        would silently stop framing, and this is the one call in the read path
        where that would look like a bridge that went quiet.
        """
        out: list[bytes | OverlongLine] = []
        data = chunk
        while data:
            newline = data.find(b"\n")
            if newline < 0:
                self._absorb(data)
                return out
            segment, data = data[:newline], data[newline + 1 :]
            self._absorb(segment)
            out.append(self._finish())
        return out

    def _absorb(self, segment: bytes) -> None:
        if self._dropping:
            self._dropped += len(segment)
            return
        if len(self._buffer) + len(segment) > self._limit:
            self._dropped = len(self._buffer) + len(segment)
            self._buffer = bytearray()
            self._dropping = True
            return
        self._buffer += segment

    def _finish(self) -> bytes | OverlongLine:
        if self._dropping:
            dropped = self._dropped
            self._dropping = False
            self._dropped = 0
            return OverlongLine(dropped_bytes=dropped)
        line = bytes(self._buffer).removesuffix(b"\r")
        self._buffer = bytearray()
        return line


# ---------------------------------------------------------------------------
# the platform shapes, constructed explicitly
# ---------------------------------------------------------------------------

#: ``CREATE_NO_WINDOW``. Spelled out rather than imported: the constant exists
#: on ``subprocess`` only on Windows, and this module is type-checked and tested
#: on Linux, where the name is absent and the behaviour it guards is invisible.
CREATE_NO_WINDOW: Final = 0x0800_0000


def creation_flags(system: str) -> int:
    """The ``creationflags`` a :class:`subprocess.Popen` gets on *system*.

    On Windows the bridge is a console program, and a console program launched
    from a process without a console gets one of its own — a black window over a
    full-screen game, every restart. ``CREATE_NO_WINDOW`` suppresses it. On
    every other platform the flag does not exist and the value must be zero,
    because ``Popen`` rejects a non-zero ``creationflags`` off Windows.

    *system* is passed in rather than read from :data:`sys.platform` so the
    Windows answer is constructible, and therefore assertable, on Linux.
    """
    return CREATE_NO_WINDOW if system.startswith("win") else 0


def program_is_path(program: str) -> bool:
    """Whether *program* names a location rather than a bare command name.

    Written for the Windows spellings on purpose, because that is where this
    ships: a backslash separates, a forward slash also separates (the Win32 API
    accepts both), and ``C:bridge.exe`` is drive-relative — a path that contains
    no separator at all and that a naive "is there a slash in it" test reads as
    a bare command name to look up on PATH.
    """
    return "/" in program or "\\" in program or PureWindowsPath(program).drive != ""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

#: How much of stdout is taken per read. One page: large enough that a chatty
#: recogniser is not a syscall per word, small enough to be a fixed cost.
_CHUNK_BYTES: Final = 4096

#: How long :meth:`TeamONBridgeClient.transcripts` parks in a worker thread
#: before looping. Not a timeout on anything — it is how quickly the stream
#: notices that the bridge died or that the session was closed.
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

#: What a refused or failed synthesis becomes. Named on this side rather than
#: taken from whatever the bridge wrote about it: the outcome already says the
#: sentence was not spoken, and the words it used to say so are its own.
_SYNTHESIS_FAULT: Final = BridgeFault(code=BridgeFaultCode.SYNTHESIS)


def _is_credential_name(name: str) -> bool:
    folded = name.casefold()
    return any(word in folded for word in _CREDENTIAL_WORDS)


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """How to launch the bridge, and every bound it runs under.

    There is no credential field, and there is no way to add one: a *name* that
    looks like a secret is refused in the mapping this is loaded from, in the
    environment it passes on, and in the option-shaped arguments it launches
    (``--api-key=…`` and its neighbours). That is a real constraint on the
    bridge program rather than a gesture — whatever it needs to authenticate, it
    reads itself, and this process stays a component that holds no key and
    therefore cannot leak one.

    The refusals name the offending *key*, never its value: the name is what the
    user has to rename, and the value is the thing that must not be written
    down.

    What is *not* claimed: a bare positional argument that happens to be a
    secret cannot be recognised as one, by this or by anything else. The rule
    the check enforces is that no credential is ever named here; the rule the
    design enforces is that nothing in this package ever needs one.
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
            # Only option-shaped arguments are checked: a program *path* may
            # legitimately contain "key" or "auth", and refusing to launch a
            # bridge because it lives in a directory with an unlucky name is a
            # check that gets switched off rather than obeyed.
            option = token.split("=", 1)[0]
            if token.startswith("-") and _is_credential_name(option):
                raise ValueError(
                    f"a bridge command line may not carry a credential, and the option "
                    f"{option!r} looks like one; the bridge program reads its own"
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
        return cls(
            command=tuple(str(token) for token in raw_command),
            env=MappingProxyType(_string_mapping(data.get("env", {}))),
            startup_timeout=_number(data, "startup_timeout", 5.0),
            reply_timeout=_number(data, "reply_timeout", 5.0),
            speech_timeout=_number(data, "speech_timeout", 30.0),
            stop_timeout=_number(data, "stop_timeout", 1.0),
            max_restarts=int(_number(data, "max_restarts", 3)),
            max_pending=int(_number(data, "max_pending", 64)),
        )

    def child_environment(self) -> dict[str, str]:
        """Exactly what the child process gets: an allowlist, plus ``env``."""
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

    ``resolved`` is a path, and it is a *field* rather than a sentence for that
    reason: the caller decides whether to show it. ``detail`` is the sentence,
    and it never contains one — it is the string that ends up in a log and in a
    bug report.
    """

    configured: bool
    available: bool
    resolved: str
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
            resolved="",
            detail="the voice bridge is not configured; voice runs without it",
        )
    program = config.command[0]
    resolved = which(program)
    if resolved is None and program_is_path(program) and Path(program).is_file():
        # `which` answers for names on PATH and for paths it considers
        # executable; an absolute path to a script the user launches through an
        # interpreter is still a file that exists, and saying otherwise would
        # send them looking for the wrong problem.
        resolved = program
    if resolved is None:
        return BridgeCheck(
            configured=True,
            available=False,
            resolved="",
            detail=(
                "the configured voice bridge program was not found on PATH and is not a file; "
                "check the 'command' setting in the voice configuration"
            ),
        )
    return BridgeCheck(
        configured=True,
        available=True,
        resolved=resolved,
        detail="the voice bridge program was found",
    )


# ---------------------------------------------------------------------------
# supervision
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """One thing about the bridge worth telling a log, and sometimes the user.

    ``detail`` is a sentence written in this module, for a log. Nothing from the
    far end reaches it: not a transcript, not a fault's ``detail``, not an
    unrecognised type name. :meth:`spoken` returns a sentence from the closed
    phrase table or nothing at all — what the user hears is never a diagnostic.
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


#: Where reports go. Synchronous, and called from the reader thread as well as
#: from the caller's, so an implementation must not block.
BridgeListener: TypeAlias = Callable[[BridgeReport], None]


class BridgeError(RuntimeError):
    """Something went wrong with the bridge, in a way the caller must see."""


class BridgeUnavailable(BridgeError):
    """The bridge is not there: never launched, crashed for good, or stopped."""


class BridgeTimeout(BridgeError):
    """The bridge is running and did not answer within its deadline."""


class BridgeFailed(BridgeError):
    """The bridge reported a failure of its own.

    The message is the fault's summary from the closed phrase table — never the
    ``detail`` the bridge sent, which is dropped at decode time.
    """

    def __init__(self, fault: BridgeFault) -> None:
        super().__init__(fault.summary())
        self.fault = fault


@dataclass(frozen=True, slots=True)
class _Down:
    """The reader saw the far end end. Wakes whoever is waiting for a reply."""

    reason: BridgeReason
    detail: str
    fatal: bool


_Event: TypeAlias = "BridgeMessage | _Down"


def _now() -> float:
    return time.monotonic()


def _deadline(timeout: float) -> float:
    return _now() + min(timeout, MAX_TIMEOUT_SECONDS)


class JsonlBridge:
    """Supervises one bridge process and the JSONL conversation with it.

    Synchronous on purpose. Everything below it is blocking — a pipe, a process,
    a thread — and wrapping blocking calls in coroutines does not make them
    interruptible, it only makes the blocking harder to see.
    :class:`TeamONBridgeClient` is the async face, and it is a thin one.
    """

    def __init__(
        self,
        config: BridgeConfig,
        *,
        listener: BridgeListener | None = None,
        system: str | None = None,
    ) -> None:
        self._config = config
        self._listener = listener
        #: Which platform's launch flags to use. Injected so a test can build
        #: the Windows shape on Linux; defaults to the platform in hand.
        self._system = sys.platform if system is None else system
        self._state = BridgeState.STOPPED
        self._process: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=config.max_pending)
        self._events: queue.Queue[_Event] = queue.Queue(maxsize=config.max_pending)
        self._transcripts: queue.Queue[BridgeTranscript] = queue.Queue(maxsize=config.max_pending)
        self._restarts = 0
        #: Bumped on every launch and every stop. A reader or writer thread left
        #: over from a previous child compares it before touching any queue, so
        #: a straggler cannot inject a transcript into the next session.
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
                handshake, or it is dead for good.
            BridgeTimeout: it never said ``ready`` within ``startup_timeout``.
            BridgeFailed: it answered the handshake with a fault.
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
        once, and every join and every wait carries a timeout. This is the
        method a panic stop reaches, so it may not have an unbounded step in it.
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
            BridgeUnavailable: the bridge is gone, or the outbox is full — which
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
            BridgeUnavailable: it went away mid-exchange.
            ProtocolMismatch: it answered in a MAJOR version this build cannot
                read.
        """
        self.send(message)
        deadline = _deadline(timeout)
        while True:
            event = self._take_event(deadline)
            if isinstance(event, _Down):
                self._handle_down(event)
            elif event.type is BridgeMessageType.ERROR:
                raise self._fault(event, BridgeReason.FAULT, "reported")
            elif event.type is BridgeMessageType.OUTCOME:
                outcome = read_outcome(event)
                if outcome.request_id == request_id:
                    return outcome
                # An outcome for something else — a reply to a request that
                # already timed out. Dropping it is right, and the loop stays
                # bounded because the deadline does not move.

    def next_transcript(self, timeout: float) -> BridgeTranscript | None:
        """The next recognition result, or ``None`` if none arrived in time.

        ``None`` is not an error: silence is the normal state of a microphone.
        """
        self._ensure_running()
        try:
            return self._transcripts.get(timeout=max(0.0, min(timeout, MAX_TIMEOUT_SECONDS)))
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
                # A pipe nobody drains is a pipe that fills and wedges the
                # child. The bridge reports through `error` messages, which are
                # typed and bounded; its own diagnostics are its own business.
                stderr=subprocess.DEVNULL,
                env=self._config.child_environment(),
                bufsize=0,
                close_fds=True,
                creationflags=creation_flags(self._system),
            )
        except (OSError, ValueError) as exc:
            # ValueError is what `Popen` raises for a non-zero `creationflags`
            # off Windows, which is what an injected platform string produces on
            # a Linux test machine. It is a launch failure like any other, and
            # reporting it as one keeps the state machine out of STARTING.
            detail = (
                "the configured voice bridge program could not be launched "
                f"({type(exc).__name__}); check the 'command' setting in the voice configuration"
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
            # A handshake that did not finish leaves nothing usable behind: the
            # process goes, and the state says stopped rather than starting, so
            # the next call reports "not running" instead of waiting on a child
            # that was never going to answer. A bridge already declared DEAD
            # keeps that state — a failed handshake does not resurrect it.
            self._terminate(process)
            self._process = None
            if self._state is not BridgeState.DEAD:
                self._state = BridgeState.STOPPED
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
            event = self._take_event(deadline)
            if isinstance(event, _Down):
                self._handle_down(event)
            elif event.type is BridgeMessageType.READY:
                return event
            elif event.type is BridgeMessageType.ERROR:
                raise self._fault(event, BridgeReason.HANDSHAKE_FAILED, "failed to start")

    def _fault(self, event: BridgeMessage, reason: BridgeReason, phrase: str) -> BridgeFailed:
        fault = read_error(event)
        self._report(
            BridgeReport(
                state=self._state,
                reason=reason,
                detail=f"the voice bridge {phrase} with a {fault.code} fault",
                fault=fault,
            )
        )
        return BridgeFailed(fault)

    def _take_event(self, deadline: float) -> _Event:
        """The next event, or :class:`BridgeTimeout` when the deadline passes.

        The remaining time is recomputed on every call, so a loop that discards
        an event it did not want — a late outcome for an abandoned request —
        cannot extend the caller's wait past the deadline it set.
        """
        remaining = deadline - _now()
        if remaining <= 0:
            raise BridgeTimeout("the voice bridge did not answer within its deadline")
        try:
            return self._events.get(timeout=remaining)
        except queue.Empty:
            raise BridgeTimeout("the voice bridge did not answer within its deadline") from None

    def _handle_down(self, down: _Down) -> None:
        if down.fatal:
            self._die(down.reason, down.detail)
            if down.reason is BridgeReason.VERSION_MISMATCH:
                raise ProtocolMismatch(down.detail)
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
                f"the voice bridge stopped {self._restarts} times and has been restarted as "
                "often as it may be; voice is off until it is fixed and the sidecar restarted"
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
        self._generation += 1
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
                # uninterruptible syscall, or a debugger attached to it.
                # Reporting a stop that did not finish is better than a stop
                # that never returns, and this is the panic path.
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=self._config.stop_timeout)
        # Closed after the child is gone, so no thread should still be parked on
        # them. ValueError and RuntimeError are what CPython raises when a
        # buffered stream is closed while another thread happens to be inside
        # it; the child is already dead by then, so there is nothing left to do
        # about either of them and losing the close is smaller than a stop path
        # that raises.
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError, ValueError, RuntimeError):
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
        # Wakes a writer thread parked on `get`. The queue was just emptied, so
        # this cannot itself block; the writer that takes it returns, and the
        # next launch gets a fresh outbox of its own.
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
            except (OSError, ValueError, RuntimeError):
                # The pipe is gone: the bridge exited, or the stop path closed
                # it under this thread. Either way the reader is the one that
                # reports it, and a second report of the same event would only
                # be noise.
                return

    def _read_loop(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        framer = LineFramer()
        while True:
            try:
                chunk = stdout.read(_CHUNK_BYTES)
            except (OSError, ValueError, RuntimeError):
                # The pipe was closed under this thread by the stop path.
                # Falling out is right; the generation check below suppresses
                # the report, because a stop is not a crash.
                break
            if not chunk:
                break
            for item in framer.feed(chunk):
                if self._generation != generation:
                    return
                self._admit(item, generation)
        if self._generation != generation:
            return
        detail = "the voice bridge closed its output; it has stopped"
        self._report(BridgeReport(state=self._state, reason=BridgeReason.CRASHED, detail=detail))
        self._offer_event(
            _Down(reason=BridgeReason.CRASHED, detail=detail, fatal=False), generation
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
            # Fatal, and no restart fixes it: the far end is a bridge for
            # another build. `str(exc)` is this module's own sentence, built
            # from two integers its own regex extracted.
            self._report(
                BridgeReport(
                    state=self._state, reason=BridgeReason.VERSION_MISMATCH, detail=str(exc)
                )
            )
            self._offer_event(
                _Down(reason=BridgeReason.VERSION_MISMATCH, detail=str(exc), fatal=True),
                generation,
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


class TeamONBridgeClient:
    """A :class:`~pz_agent_voice.adapters.teamon.TeamONClient` over the bridge.

    The three methods the adapter needs, each mapped onto one message type, and
    each running its blocking half in a worker thread so the companion's event
    loop keeps turning while a sentence is being spoken. Beyond them,
    :meth:`submit_goal` carries a goal token across and waits for the outcome,
    which is how the companion learns that the goal *ended* rather than assuming
    it did.
    """

    def __init__(
        self,
        config: BridgeConfig,
        *,
        listener: BridgeListener | None = None,
        system: str | None = None,
    ) -> None:
        self._bridge = JsonlBridge(config, listener=listener, system=system)
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
            except (BridgeError, BridgeProtocolError):
                # The bridge is gone and has already been reported — with a
                # sentence for the user, if it died rather than being stopped.
                # Ending the iterator ends the session, which is the contract.
                #
                # `BridgeProtocolError` is caught for the same reason and is not
                # a `BridgeError`: a relaunch on this path can meet a *different*
                # program — the one on disk was replaced between the crash and
                # the restart — and `ProtocolMismatch` then comes out of
                # `next_transcript` by way of the handshake. Letting it out would
                # end the session by raising through an `async for`, which is not
                # the same thing as ending it, and the bridge has already been
                # declared dead and announced by the time it arrives.
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
            BridgeUnavailable: the bridge went away mid-sentence.
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
        one; the utterance is abandoned on this side either way. The send itself
        is a queue put that refuses when full, so this never waits on the pipe.

        *Never* means every failure this call has, not only the ones that are
        :class:`BridgeError`. :class:`BridgeProtocolError` is not one — it is a
        ``ValueError`` — and two of them reach here: a ``utterance_id`` that is
        not a handle, and a :class:`ProtocolMismatch` raised by the handshake
        when the send restarts a crashed bridge and the replacement turns out to
        speak another MAJOR. Both are caught, because a stop word that raises is
        a stop word that did not happen.
        """
        try:
            await asyncio.to_thread(self._bridge.send, interrupt_message(utterance_id))
        except (BridgeError, BridgeProtocolError):
            return

    async def submit_goal(self, *, request_id: str, goal: VoiceGoal) -> BridgeOutcome:
        """Dispatch *goal* and wait for the bridge to say how it ended."""
        message = goal_message(request_id=request_id, goal=goal)
        return await asyncio.to_thread(
            self._exchange, message, request_id, self._config.reply_timeout
        )

    async def aclose(self) -> None:
        """Stop the bridge. Bounded, exactly like :meth:`JsonlBridge.stop`."""
        await asyncio.to_thread(self._bridge.stop)

    def _exchange(self, message: BridgeMessage, request_id: str, timeout: float) -> BridgeOutcome:
        return self._bridge.exchange(message, request_id=request_id, timeout=timeout)
