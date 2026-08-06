"""The transport contract: what a speech backend has to provide.

Three methods, and the whole package is written against them so that swapping
TeamON for anything else is a new class rather than a change to the companion.

The signature of :meth:`VoiceAdapter.events` is the blueprint's (§ 11.4) and is
worth reading carefully: it is a coroutine that *returns* an async iterator, not
an async generator. The distinction is load bearing for the type checker — an
``async def`` with a ``yield`` in it has type ``AsyncIterator``, not
``Coroutine[..., AsyncIterator]`` — so an implementation keeps its generator in
a second method and returns it from here. In exchange, obtaining the stream is
itself awaitable, which is what a backend that has to open a socket or a
microphone needs.

:class:`SpeechCancelled` is how an adapter reports that an utterance was cut
short. Raising is optional: :mod:`.driver` also tracks the cancellation it
requested, so an adapter whose ``speak`` simply returns early is handled
identically. What an adapter must *not* do is return normally from ``speak``
after an interruption and leave the caller believing the sentence was heard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .messages import VoiceInput, VoiceOutput

__all__ = ["SpeechCancelled", "VoiceAdapter"]


class SpeechCancelled(Exception):
    """An utterance was interrupted before it finished."""

    def __init__(self, utterance: VoiceOutput) -> None:
        super().__init__(f"speech cancelled while saying {utterance.topic!r}")
        self.utterance = utterance


class VoiceAdapter(Protocol):
    """Speech in, speech out, and the lever that stops speech out."""

    async def events(self) -> AsyncIterator[VoiceInput]:
        """Open the transcript stream.

        The returned iterator ends when the backend closes. It must yield
        interim transcripts as well as final ones: barge-in and the stop word
        both depend on hearing the user before the recogniser has settled.
        """
        ...

    async def speak(self, message: VoiceOutput) -> None:
        """Say *message*, returning when it has been delivered or cut short.

        Raises:
            SpeechCancelled: optionally, when :meth:`cancel_speech` interrupted
                this utterance. Returning normally is a claim that the user
                heard the whole sentence.
        """
        ...

    async def cancel_speech(self) -> None:
        """Stop the current utterance now.

        Must be safe to call when nothing is being spoken, and must not wait for
        the current sentence to reach a natural boundary — this is the call that
        makes barge-in feel immediate, and every millisecond of it is a
        millisecond the user is talking over a machine.
        """
        ...
