"""The async half: pump transcripts in, pump speech out, and interrupt on demand.

Two tasks, because the alternative does not work. If speech were awaited on the
same task that reads transcripts, the companion would be deaf for exactly as
long as it was talking — and the one thing a user most needs to say while it is
talking is "стоп". So :meth:`VoiceCompanion.run` reads the event stream while a
second task drains the utterance queue, and the first thing an incoming
transcript does, before it is even classified, is cancel whatever the second one
is saying. That is barge-in, and it costs one ``await`` on the adapter.

The two tasks are sequenced by a gate rather than by chance. While a transcript
is being handled the speech pump does not start a new utterance, so the state
the pump observes is never half-updated: a stop empties the queue *and* the pump
sees the empty queue, in that order, without a scheduling race in between.

Nothing here sleeps or polls. The pump blocks on an event that the input path
sets, and the input path blocks on the adapter's stream. The companion also
subscribes to the session's own TTS stream, so speech the *sidecar* enqueued —
a plan finishing, a manual takeover — wakes the pump without waiting for the
user to say something first.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Final

from pz_agent_core.ipc.clocks import Clock

from .adapter import SpeechCancelled, VoiceAdapter
from .events import TtsEvent, TtsEventKind
from .messages import VoiceInput, VoiceOutput
from .session import VoiceSession
from .state import VoiceTurn

__all__ = ["MAX_SPEECH_FAILURES", "MAX_TURN_HISTORY", "VoiceCompanion"]

#: Turns kept for diagnostics. A session runs for hours; the last few dozen are
#: what a support bundle needs, and everything older is a leak.
MAX_TURN_HISTORY: Final = 32

#: Synthesiser failures kept for diagnostics, bounded for the same reason.
MAX_SPEECH_FAILURES: Final = 16

#: Stream events that mean there may now be something to say.
_PENDING_KINDS: Final[frozenset[TtsEventKind]] = frozenset(
    {TtsEventKind.QUEUED, TtsEventKind.SUPERSEDED}
)


class VoiceCompanion:
    """Drives a :class:`~.session.VoiceSession` over a :class:`~.adapter.VoiceAdapter`."""

    def __init__(self, adapter: VoiceAdapter, session: VoiceSession, *, clock: Clock) -> None:
        self._adapter = adapter
        self._session = session
        self._clock = clock
        self._speaking: VoiceOutput | None = None
        self._interrupted = False
        self._closing = False
        #: Closed while a transcript is being handled, so the pump cannot start
        #: speaking out of a state that is mid-update.
        self._gate = asyncio.Event()
        self._gate.set()
        #: Set whenever something may have been added to the queue.
        self._work = asyncio.Event()
        self._turns: deque[VoiceTurn] = deque(maxlen=MAX_TURN_HISTORY)
        self._speech_failures: deque[str] = deque(maxlen=MAX_SPEECH_FAILURES)
        self._unsubscribe = session.events.subscribe(self._on_stream_event)

    def wake(self) -> None:
        """Tell the pump that the queue may have gained something.

        Called for you by the subscription; exposed for a caller that pushes to
        the queue through some other route and must not have to know that.
        """
        self._work.set()

    def _on_stream_event(self, event: TtsEvent) -> None:
        if event.kind in _PENDING_KINDS:
            self._work.set()

    @property
    def speaking(self) -> VoiceOutput | None:
        """The utterance currently being spoken, or None."""
        return self._speaking

    @property
    def turns(self) -> tuple[VoiceTurn, ...]:
        """The most recent turns, oldest first, bounded by :data:`MAX_TURN_HISTORY`."""
        return tuple(self._turns)

    @property
    def last_turn(self) -> VoiceTurn | None:
        return self._turns[-1] if self._turns else None

    @property
    def speech_failures(self) -> tuple[str, ...]:
        """Synthesiser failures, in the order they happened, bounded.

        A backend that raises is a backend the user did not hear. The utterance
        is reported cancelled rather than finished and the reason is kept here,
        because "the companion went quiet" with nothing recorded is the failure
        mode a support bundle cannot explain.
        """
        return tuple(self._speech_failures)

    async def run(self) -> None:
        """Serve the conversation until the adapter's stream ends."""
        pump = asyncio.create_task(self._speech_pump())
        cancelled = False
        try:
            stream = await self._adapter.events()
            async for raw in stream:
                await self.deliver(raw)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # On a stream that ended, the pump is woken rather than cancelled:
            # an utterance already in flight has been heard by the user, and
            # abandoning it mid-word on shutdown would report speech that never
            # finished as never started.
            self._closing = True
            self._gate.set()
            self._work.set()
            if cancelled:
                # On a cancellation it is the other way round. The caller asked
                # to stop now, and a backend parked inside speak() may never
                # return — waiting for it would hang the shutdown on exactly the
                # component that just failed to behave.
                pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                if not cancelled:
                    raise
            finally:
                # Unconditional: a subscription that outlives its companion is a
                # slot the next one cannot have, and the stream's cap is small.
                self._unsubscribe()

    async def deliver(self, raw: VoiceInput) -> VoiceTurn:
        """Handle one transcript, barging in on any speech first.

        Exposed separately from :meth:`run` because the panic hotkey and the
        diagnostics harness both need to inject one input without owning the
        adapter's stream.

        The barge-in happens *before* the transcript is classified, not after.
        Waiting to find out what the user said before stopping talking over them
        would put the classifier's work — and, for an interim transcript, the
        recogniser's remaining latency — inside the gap the user experiences.
        """
        self._gate.clear()
        try:
            if self._speaking is not None:
                await self._barge_in()
            turn = self._session.handle(raw)
            self._turns.append(turn)
            return turn
        finally:
            self._gate.set()
            self._work.set()

    async def _barge_in(self) -> None:
        self._interrupted = True
        await self._adapter.cancel_speech()

    async def _speech_pump(self) -> None:
        while True:
            await self._gate.wait()
            message = self._session.queue.pop()
            if message is None:
                if self._closing:
                    return
                # Cleared before waiting, and only ever after observing an empty
                # queue with no await in between, so a push that happens later
                # cannot be missed and one that already happened is not re-armed.
                self._work.clear()
                await self._work.wait()
                continue
            await self._speak(message)

    async def _speak(self, message: VoiceOutput) -> None:
        self._speaking = message
        self._interrupted = False
        self._publish(TtsEventKind.STARTED, message)
        try:
            await self._adapter.speak(message)
        except SpeechCancelled:
            self._interrupted = True
        except asyncio.CancelledError:
            self._speaking = None
            self._publish(TtsEventKind.CANCELLED, message)
            raise
        except Exception as exc:
            # A backend that failed did not deliver the sentence, so the event
            # is a cancellation, not a finish. The pump survives it: a
            # synthesiser that breaks once must not leave the companion mute
            # for the rest of the session, and it must certainly not take the
            # acknowledgement of the next stop down with it.
            self._interrupted = True
            self._speech_failures.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._speaking = None
        self._publish(
            TtsEventKind.CANCELLED if self._interrupted else TtsEventKind.FINISHED, message
        )

    def _publish(self, kind: TtsEventKind, message: VoiceOutput) -> None:
        self._session.events.publish(TtsEvent(kind=kind, at_ms=self._clock(), utterance=message))
