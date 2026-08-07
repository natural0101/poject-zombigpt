"""The wire the voice bridge speaks: one JSON object per line, and nothing else.

The bridge is a *separate process*. That is the whole reason this module is
written the way it is: everything arriving on the pipe was produced by code this
build does not own, on a machine where a vendor SDK, a Python of another version
and a microphone driver all get a say, and the process reading it has a
loudspeaker attached to it. So the reader is written as if the far end were
hostile, and the three properties it holds are the ones a hostile far end would
attack.

**Framed by newline, one object per line.** :class:`LineFramer` splits the byte
stream and :func:`encode` guarantees the other direction: ``json.dumps`` escapes
every control character, so a newline *inside* a field is ``\\n`` in the output
and cannot end the line early. A message therefore never spans two lines and a
line never carries two messages.

**Bounded before it is parsed.** A line longer than :data:`MAX_LINE_BYTES` is
dropped as it arrives rather than accumulated: a bridge that emits a gigabyte
without a newline must cost this process a fixed buffer, not a gigabyte. The
same cap applies to what is sent, so no legal message can be refused by the
reader on the other side.

**A closed set of types, checked against a declared direction.** Anything else
is refused, loudly, and the reader carries on. Refusing rather than ignoring is
deliberate: an ignored type is a silent protocol drift that shows up months
later as a feature that quietly never worked.

**Message types — this is the complete set.**

To the bridge:

``hello``
    Opens the session and declares this build's protocol version. Fields: none
    beyond the envelope.
``speak``
    One utterance to synthesise. Fields: ``utterance_id``, ``text``,
    ``priority`` (0 is most urgent), ``interruptible``.
``interrupt``
    Abandon ``utterance_id`` now, without waiting for a phrase boundary.
``goal``
    One member of :class:`~pz_agent_voice.messages.VoiceGoal`, dispatched under
    a ``request_id``. A token, never transcript text — the channel from a
    microphone to anything else is this enum and nothing wider.

From the bridge:

``ready``
    Answers ``hello`` and declares the bridge's protocol version. Until it
    arrives the session has not started.
``transcript``
    One recognition result. Fields: ``text``, ``at_ms``, ``final``,
    ``confidence``. Interim hypotheses included — the stop word depends on them.
``outcome``
    A ``speak`` or a ``goal`` ended, and how. Fields: ``request_id``,
    ``status``. This is how the companion learns that a goal *finished* rather
    than assuming it did; without it, "готово" would be a guess.
``error``
    The bridge failed. Field: ``code``, plus an optional ``detail`` that is read
    to classify the fault and then **discarded** — see :func:`read_error`.

The envelope carries ``v``, the protocol version, on every message in both
directions. A MAJOR mismatch is refused (:class:`ProtocolMismatch`) because the
two sides then disagree about what the fields mean; a MINOR difference is
accepted, which is what makes adding a field to this table a compatible change.
"""

from __future__ import annotations

# The sentences in FAULT_PHRASES and BRIDGE_UNAVAILABLE_PHRASE are spoken
# Russian, short enough that every letter in them has a Latin lookalike. Taking
# the confusable-character rule's suggestion would leave a synthesiser reading
# out a word that is not a word.
# ruff: noqa: RUF001
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..messages import MAX_TEXT_CHARS, MAX_TRANSCRIPT_CHARS, VoiceGoal

__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "BRIDGE_UNAVAILABLE_PHRASE",
    "FAULT_PHRASES",
    "MAX_LINE_BYTES",
    "MAX_MESSAGE_BYTES",
    "MESSAGE_DIRECTIONS",
    "BridgeDirection",
    "BridgeFault",
    "BridgeFaultCode",
    "BridgeMessage",
    "BridgeMessageType",
    "BridgeOutcome",
    "BridgeProtocolError",
    "BridgeTranscript",
    "LineFramer",
    "MalformedMessage",
    "MessageTooLarge",
    "OutcomeStatus",
    "OverlongLine",
    "ProtocolMismatch",
    "UnknownMessageType",
    "decode",
    "encode",
    "goal_message",
    "hello_message",
    "interrupt_message",
    "read_error",
    "read_outcome",
    "read_ready",
    "read_transcript",
    "speak_message",
]

#: MAJOR.MINOR. MAJOR changes when a field changes meaning or disappears; MINOR
#: changes when one is added. Both sides send it on every message so a mismatch
#: is caught on the first line rather than on the first field that differs.
BRIDGE_PROTOCOL_VERSION: Final = "1.0"

#: The largest message this side will send. Every legal message is a handful of
#: short fields — the longest is a ``speak`` carrying at most
#: :data:`~pz_agent_voice.messages.MAX_TEXT_CHARS` characters — so this is two
#: orders of magnitude of headroom and still a fixed number of bytes.
MAX_MESSAGE_BYTES: Final = 16_384

#: The longest line the reader will assemble. Equal to the send cap on purpose:
#: a line that cannot hold a legal message is not worth buffering, so the reader
#: can drop it the moment it passes the cap instead of waiting for a newline
#: that may never come.
MAX_LINE_BYTES: Final = MAX_MESSAGE_BYTES

#: How much of an unrecognised type name is quoted back in a refusal. The name
#: came from another process; it is bounded before it reaches a log line.
_MAX_QUOTED_CHARS: Final = 32

#: Correlation handles are minted by this side (``teamon-3``) and are shape
#: checked rather than sanitised, so a handle can never carry punctuation into
#: a JSON field or a log.
_HANDLE: Final = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")

_VERSION: Final = re.compile(r"^(\d{1,3})\.(\d{1,4})$")


class BridgeMessageType(StrEnum):
    """Every message this protocol has. Anything else is refused."""

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
    """The two sides disagree about the MAJOR version, so nothing else is safe."""


class OutcomeStatus(StrEnum):
    """How a ``speak`` or a ``goal`` ended.

    ``ENDED`` is the only member that claims the thing was carried out. The
    other three are all "it stopped", distinguished because the companion says
    something different about each and because a refusal is not a failure.
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
        MalformedMessage: when it is not two small integers separated by a dot.
    """
    match = _VERSION.match(raw)
    if match is None:
        return _reject_version(raw)
    return int(match.group(1)), int(match.group(2))


def _reject_version(raw: str) -> tuple[int, int]:
    raise MalformedMessage(
        f"the protocol version must look like MAJOR.MINOR, got {raw[:_MAX_QUOTED_CHARS]!r}"
    )


def check_protocol_version(raw: str) -> tuple[int, int]:
    """Accept *raw* if its MAJOR matches this build's.

    Raises:
        MalformedMessage: the version is not MAJOR.MINOR.
        ProtocolMismatch: the MAJOR differs, so the field names on the wire no
            longer mean what this build thinks they mean.
    """
    reported = parse_version(raw)
    ours = parse_version(BRIDGE_PROTOCOL_VERSION)
    if reported[0] != ours[0]:
        raise ProtocolMismatch(
            f"the bridge speaks protocol {raw[:_MAX_QUOTED_CHARS]} and this build speaks "
            f"{BRIDGE_PROTOCOL_VERSION}; install a bridge built for {ours[0]}.x"
        )
    return reported


def encode(message: BridgeMessage) -> bytes:
    """One message as one line, newline included.

    ``json.dumps`` escapes control characters, so a field containing a newline
    is written as ``\\n`` and the line still ends exactly once, at the end.

    Raises:
        MessageTooLarge: the encoded form is past :data:`MAX_MESSAGE_BYTES`.
        MalformedMessage: a field is not JSON-representable.
    """
    body = {"v": BRIDGE_PROTOCOL_VERSION, "type": str(message.type), **message.fields}
    try:
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MalformedMessage(f"{message.type} carries a field that is not JSON: {exc}") from exc
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
        # `exc.msg` is the parser's own wording ("Expecting value") and carries
        # none of the line; the line itself is never quoted, because the far end
        # chooses its content and this string reaches a log.
        raise MalformedMessage(
            f"a line that is not JSON ({exc.msg} at column {exc.colno})"
        ) from exc
    if not isinstance(body, dict):
        raise MalformedMessage(f"a message must be a JSON object, got {type(body).__name__}")
    kind = _message_type(body.get("type"), expect)
    version = body.get("v")
    if not isinstance(version, str):
        raise MalformedMessage(f"a {kind} message must carry the protocol version in 'v'")
    check_protocol_version(version)
    return BridgeMessage(type=kind, fields=dict(body))


def _message_type(raw: object, expect: BridgeDirection) -> BridgeMessageType:
    if not isinstance(raw, str):
        raise MalformedMessage("a message must carry a string 'type'")
    try:
        kind = BridgeMessageType(raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in BridgeMessageType))
        raise UnknownMessageType(
            f"{raw[:_MAX_QUOTED_CHARS]!r} is not a message type this build has; known types "
            f"are {known}"
        ) from exc
    if MESSAGE_DIRECTIONS[kind] is not expect:
        raise UnknownMessageType(
            f"a {kind} message travels {MESSAGE_DIRECTIONS[kind]}, and this line arrived "
            f"as {expect}"
        )
    return kind


# ---------------------------------------------------------------------------
# what this side sends
# ---------------------------------------------------------------------------


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
            negative. Refused here rather than truncated: an utterance the queue
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


def _check_handle(value: str, field: str) -> None:
    if not _HANDLE.match(value):
        raise MalformedMessage(
            f"{field} must be a lowercase handle of at most 64 characters, "
            f"got {value[:_MAX_QUOTED_CHARS]!r}"
        )


# ---------------------------------------------------------------------------
# what this side reads
# ---------------------------------------------------------------------------


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
        raise MalformedMessage("an outcome must name the request it belongs to")
    raw = message.fields.get("status")
    if not isinstance(raw, str):
        raise MalformedMessage("an outcome must carry 'status' as a string")
    try:
        status = OutcomeStatus(raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in OutcomeStatus))
        raise MalformedMessage(
            f"{raw[:_MAX_QUOTED_CHARS]!r} is not an outcome status; known ones are {known}"
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


def _read_int(value: object, field: str) -> int:
    # `bool` is an `int` in Python, and `True` as a timestamp is a bug that
    # would otherwise read as one millisecond.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedMessage(f"{field} must be a whole number")
    return value


def _read_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedMessage(f"{field} must be a number")
    return float(value)


class LineFramer:
    """Splits a byte stream into lines, dropping any line past the cap.

    The dropping is what makes this bounded, and it happens *as the bytes
    arrive*: once the current line has passed the cap the framer stops keeping
    any of it and skips forward to the next newline. A bridge emitting one
    enormous line therefore costs a fixed buffer and a counter, and the line
    after it still parses — which is the recovery the reader depends on, since
    the alternative is a session that ends because one message was malformed.
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

        A list rather than an iterator: a generator that the caller forgot to
        drain would silently stop framing, and this is the one call in the read
        path where that would look like a bridge that went quiet.
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
        line = bytes(self._buffer).rstrip(b"\r")
        self._buffer = bytearray()
        return line
