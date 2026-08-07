"""The TeamON plugin, written against an interface instead of against a guess.

TeamON's SDK is not installed here and its surface has not been verified, so
this module does what the capability rules require everywhere else in the
project: it states exactly what it needs, in a protocol it owns, and refuses to
pretend it knows the vendor's function names. :class:`TeamONVoiceAdapter` is
complete and fully exercised against that protocol, and
:func:`require_teamon_sdk` is the one place a vendor import would happen.

**One implementation ships**, and it is a process rather than an import:
:class:`~pz_agent_voice.teamon.TeamONBridgeClient` launches a bridge program,
speaks the JSONL protocol in :mod:`pz_agent_voice.teamon` to it over stdin and
stdout, and supervises it — bounded reads, bounded waits,
counted restarts, a stop that never waits for the bridge to agree. The vendor
SDK lives on the far side of that pipe, where a crash costs a restart instead of
the sidecar. That module deliberately imports *this* one and not the other way
round: this file must keep importing and type-checking on a machine that has
neither the SDK nor a bridge.

**What an integrator provides instead**, if they would rather link the SDK
in-process than run a bridge — a :class:`TeamONClient` with:

``transcripts()``
    Awaited once, returning an async iterator of :class:`TeamONTranscript` —
    the same "open the stream, then read it" shape as
    :meth:`~pz_agent_voice.adapter.VoiceAdapter.events`, so a client that has to
    connect before it can yield may do so. It must emit interim hypotheses, not
    only settled ones: barge-in and the stop word both depend on hearing the
    user before the recogniser has endpointed. Ending the iterator ends the
    session.

``synthesize(request)``
    Speak :attr:`TeamONSpeech.text`, returning when the audio has been delivered
    or abandoned. ``priority`` is the utterance's urgency, 0 being highest, and
    ``interruptible`` is False for the stop acknowledgement, which must be heard.

``interrupt(utterance_id)``
    Abandon the named utterance immediately. Must be safe to call for an
    utterance that already finished, and must not wait for a phrase boundary.

Everything the adapter itself owns — clamping a confidence a client reported out
of range, bounding an over-long transcript, deciding that a synthesise call
which returned after an interrupt did *not* deliver its sentence — is here and
is tested, because those are the parts that would otherwise be reimplemented,
differently, by every integration.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pz_agent_core.ipc.clocks import Clock

from ..adapter import SpeechCancelled
from ..messages import MAX_TRANSCRIPT_CHARS, OutputKind, VoiceInput, VoiceOutput

__all__ = [
    "TEAMON_SDK_MODULE",
    "TeamONClient",
    "TeamONSdkUnavailable",
    "TeamONSpeech",
    "TeamONTranscript",
    "TeamONVoiceAdapter",
    "require_teamon_sdk",
    "teamon_sdk_available",
]

#: The distribution's import name. Named once so the error message, the
#: availability probe and the import cannot drift apart.
TEAMON_SDK_MODULE: Final = "teamon"


@dataclass(frozen=True, slots=True)
class TeamONTranscript:
    """One recognition result as the client reports it."""

    text: str
    at_ms: int
    final: bool = True
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class TeamONSpeech:
    """One synthesis request.

    ``utterance_id`` exists so that :meth:`TeamONClient.interrupt` names a
    specific sentence. Interrupting "whatever is playing" races with the next
    utterance starting, and the sentence that gets cut is then the wrong one.
    """

    utterance_id: str
    text: str
    priority: int
    interruptible: bool


class TeamONClient(Protocol):
    """The transport the adapter needs. Implemented over the SDK by the integrator."""

    async def transcripts(self) -> AsyncIterator[TeamONTranscript]:
        """Open the recognition stream, interim hypotheses included."""
        ...

    async def synthesize(self, request: TeamONSpeech) -> None:
        """Speak *request*, returning when it is delivered or abandoned."""
        ...

    async def interrupt(self, utterance_id: str) -> None:
        """Abandon *utterance_id* now. Safe to call when it has already ended."""
        ...


class TeamONSdkUnavailable(RuntimeError):
    """The TeamON SDK is not installed in this environment."""

    def __init__(self) -> None:
        super().__init__(
            f"the TeamON SDK is not installed; install the {TEAMON_SDK_MODULE!r} package "
            "to use the TeamON voice adapter, or supply your own TeamONClient"
        )


def teamon_sdk_available() -> bool:
    """True when the vendor package can be imported in this environment."""
    return importlib.util.find_spec(TEAMON_SDK_MODULE) is not None


def require_teamon_sdk() -> Any:
    """Import the TeamON SDK, or say plainly that it is absent.

    The only vendor import in the project. It is reported as a missing install
    step rather than an ``ImportError`` traceback because that is what the user
    has to act on, and it is confined here so that every other module in this
    package imports, type-checks and tests on a machine without the SDK.

    Raises:
        TeamONSdkUnavailable: when the package is not installed.
    """
    try:
        return importlib.import_module(TEAMON_SDK_MODULE)
    except ImportError as exc:
        raise TeamONSdkUnavailable() from exc


class TeamONVoiceAdapter:
    """A :class:`~pz_agent_voice.adapter.VoiceAdapter` over a :class:`TeamONClient`."""

    def __init__(self, client: TeamONClient, *, clock: Clock, source: str = "teamon") -> None:
        self._client = client
        self._clock = clock
        self._source = source
        self._utterances = 0
        self._current_id: str | None = None
        #: Bumped by every cancellation, so a ``synthesize`` that returns after
        #: an interrupt is recognised as having been cut short even when the
        #: client reports nothing and raises nothing.
        self._cancel_generation = 0

    @property
    def current_utterance_id(self) -> str | None:
        return self._current_id

    async def events(self) -> AsyncIterator[VoiceInput]:
        """Open the transcript stream. See :class:`~pz_agent_voice.adapter.VoiceAdapter`."""
        return self._stream(await self._client.transcripts())

    async def _stream(self, source: AsyncIterator[TeamONTranscript]) -> AsyncIterator[VoiceInput]:
        async for transcript in source:
            yield VoiceInput(
                transcript=transcript.text[:MAX_TRANSCRIPT_CHARS],
                at_ms=max(0, transcript.at_ms),
                final=transcript.final,
                # Clamped rather than trusted: a client reporting 1.7 would
                # otherwise sail past the confidence gate that exists to stop
                # the companion acting on a guess, and one reporting -0.1 would
                # be refused by VoiceInput and kill the stream.
                confidence=min(1.0, max(0.0, transcript.confidence)),
                source=self._source,
            )

    async def speak(self, message: VoiceOutput) -> None:
        self._utterances += 1
        utterance_id = f"{self._source}-{self._utterances}"
        generation = self._cancel_generation
        self._current_id = utterance_id
        request = TeamONSpeech(
            utterance_id=utterance_id,
            text=message.text,
            priority=message.kind.rank,
            # The stop acknowledgement is the one sentence that must survive:
            # it is the answer to the user's most urgent question, and there is
            # nothing more urgent that could legitimately pre-empt it.
            interruptible=message.kind is not OutputKind.STOP,
        )
        try:
            await self._client.synthesize(request)
        finally:
            self._current_id = None
        if self._cancel_generation != generation:
            raise SpeechCancelled(message)

    async def cancel_speech(self) -> None:
        self._cancel_generation += 1
        current = self._current_id
        if current is None:
            return
        await self._client.interrupt(current)
