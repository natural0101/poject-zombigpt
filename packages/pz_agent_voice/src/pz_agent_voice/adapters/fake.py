"""A speech backend made of lists and events. No IO, no timing, no surprises.

This is the adapter the whole test suite runs against, so it has to be able to
reproduce the situations that matter rather than merely satisfy the protocol.
Three of those situations shape its design:

* **Mid-sentence.** With ``hold_speech`` set, :meth:`FakeVoiceAdapter.speak`
  parks until the test either finishes the utterance or cancels it. That is the
  only way to put the companion into the state the stop requirement is really
  about — an adapter halfway through a sentence — and then interrupt it.
* **Interim transcripts.** :meth:`push` takes ``final`` and ``confidence``, so
  the barge-in path and the "do not guess" path can be driven separately.
* **Deterministic waiting.** :meth:`wait_until_speaking` is an event, not a
  poll, so a test synchronises on the adapter actually having started rather
  than on a number of scheduler turns.

Timing is a caller-supplied :data:`~pz_agent_core.ipc.clocks.Clock`. Nothing in
this module reads a real clock or sleeps.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator

from pz_agent_core.ipc.clocks import Clock

from ..adapter import SpeechCancelled
from ..messages import MAX_TRANSCRIPT_CHARS, VoiceInput, VoiceOutput

__all__ = ["FakeVoiceAdapter"]


class FakeVoiceAdapter:
    """Scriptable input, captured output, controllable timing."""

    def __init__(self, *, clock: Clock, hold_speech: bool = False) -> None:
        self._clock = clock
        self.hold_speech = hold_speech
        #: Utterances handed to :meth:`speak`, in order, whatever became of them.
        self.started: list[VoiceOutput] = []
        #: Utterances that were delivered whole.
        self.spoken: list[VoiceOutput] = []
        #: Utterances that were cut short by :meth:`cancel_speech`.
        self.cancelled: list[VoiceOutput] = []
        #: Every call to :meth:`cancel_speech`, including the ones with nothing
        #: to cancel — barge-in must be idempotent and that is worth asserting.
        self.cancel_calls = 0
        self._inbox: deque[VoiceInput] = deque()
        self._arrival = asyncio.Event()
        self._closed = False
        self._current: VoiceOutput | None = None
        self._release = asyncio.Event()
        self._speaking_now = asyncio.Event()
        self._interrupt = False

    # -- scripting ---------------------------------------------------------

    def push(
        self,
        transcript: str,
        *,
        final: bool = True,
        confidence: float = 1.0,
        at_ms: int | None = None,
    ) -> VoiceInput:
        """Queue one transcript for the stream and return it."""
        raw = VoiceInput(
            transcript=transcript[:MAX_TRANSCRIPT_CHARS],
            at_ms=self._clock() if at_ms is None else at_ms,
            final=final,
            confidence=confidence,
        )
        self._inbox.append(raw)
        self._arrival.set()
        return raw

    def script(self, *transcripts: str) -> tuple[VoiceInput, ...]:
        """Queue several final transcripts at once."""
        return tuple(self.push(text) for text in transcripts)

    def close(self) -> None:
        """End the stream once everything queued has been delivered."""
        self._closed = True
        self._arrival.set()

    # -- timing ------------------------------------------------------------

    @property
    def speaking(self) -> VoiceOutput | None:
        """The utterance :meth:`speak` is parked on, or None."""
        return self._current

    async def wait_until_speaking(self) -> VoiceOutput:
        """Block until an utterance is in flight, then return it.

        Raises:
            RuntimeError: when the adapter is not holding speech, where this
                would wait forever — a test that calls it without
                ``hold_speech`` has a bug, and hanging is a poor way to report
                one.
        """
        if not self.hold_speech:
            raise RuntimeError("wait_until_speaking requires hold_speech=True")
        await self._speaking_now.wait()
        current = self._current
        if current is None:
            raise RuntimeError("the speaking event fired with no utterance in flight")
        return current

    def finish_speech(self) -> bool:
        """Let a held utterance complete naturally. True if one was held."""
        if self._current is None:
            return False
        self._release.set()
        return True

    # -- the adapter protocol ---------------------------------------------

    async def events(self) -> AsyncIterator[VoiceInput]:
        """Open the transcript stream. See :class:`~pz_agent_voice.adapter.VoiceAdapter`."""
        return self._stream()

    async def _stream(self) -> AsyncIterator[VoiceInput]:
        while True:
            while self._inbox:
                yield self._inbox.popleft()
            if self._closed:
                return
            self._arrival.clear()
            if self._inbox or self._closed:
                # Something arrived between draining the inbox and disarming the
                # event; re-check rather than wait for an arrival that already
                # happened.
                continue
            await self._arrival.wait()

    async def speak(self, message: VoiceOutput) -> None:
        self.started.append(message)
        if not self.hold_speech:
            self.spoken.append(message)
            return
        self._current = message
        self._interrupt = False
        self._release.clear()
        self._speaking_now.set()
        try:
            await self._release.wait()
        finally:
            self._current = None
            self._speaking_now.clear()
        if self._interrupt:
            self._interrupt = False
            self.cancelled.append(message)
            raise SpeechCancelled(message)
        self.spoken.append(message)

    async def cancel_speech(self) -> None:
        self.cancel_calls += 1
        if self._current is None:
            return
        self._interrupt = True
        self._release.set()
