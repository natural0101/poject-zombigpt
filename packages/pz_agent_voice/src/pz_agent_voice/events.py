"""The TTS event stream: what happened to every utterance, for anyone watching.

A consumer — a HUD, a log, an integration test — needs to know not only what was
spoken but what was *not*: a message the queue collapsed into a newer one and a
message cancelled mid-sentence are both invisible in the audio, and both are the
interesting cases. So every transition an utterance can make is published,
including the ones that end in silence.

The stream holds two bounds. Subscribers are capped, because a leaked
subscription in a long session is a slow memory leak that only shows up after
hours; history is a ring, because it exists for "what just happened", not as an
archive. A subscriber that raises is unsubscribed and its failure recorded — one
broken consumer must not be able to stop the companion from speaking.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from .messages import VoiceOutput

__all__ = [
    "MAX_FAILURES",
    "MAX_HISTORY",
    "MAX_SUBSCRIBERS",
    "TooManySubscribers",
    "TtsEvent",
    "TtsEventKind",
    "TtsEventStream",
    "TtsListener",
]

MAX_SUBSCRIBERS: Final = 8
MAX_HISTORY: Final = 64
MAX_FAILURES: Final = 16


class TtsEventKind(StrEnum):
    """Every transition an utterance can make.

    ``COLLAPSED`` and ``SUPERSEDED`` are distinct on purpose: the first means
    the same thing was already going to be said, the second means something
    newer about the same subject replaced it. Only the second is a change of
    content, and a consumer that reports "the assistant changed its mind" needs
    to tell them apart.
    """

    QUEUED = "queued"
    COLLAPSED = "collapsed"
    SUPERSEDED = "superseded"
    DROPPED = "dropped"
    CLEARED = "cleared"
    STARTED = "started"
    CANCELLED = "cancelled"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class TtsEvent:
    """One transition. ``utterance`` is None only for :attr:`TtsEventKind.CLEARED`."""

    kind: TtsEventKind
    at_ms: int
    utterance: VoiceOutput | None = None

    def __post_init__(self) -> None:
        if self.utterance is None and self.kind is not TtsEventKind.CLEARED:
            raise ValueError(f"{self.kind.value} must name the utterance it happened to")


TtsListener: TypeAlias = Callable[[TtsEvent], None]


class TooManySubscribers(RuntimeError):
    """The stream is already at :data:`MAX_SUBSCRIBERS`."""

    def __init__(self) -> None:
        super().__init__(f"the TTS event stream accepts at most {MAX_SUBSCRIBERS} subscribers")


class TtsEventStream:
    """Bounded fan-out of :class:`TtsEvent` to synchronous listeners."""

    def __init__(self, *, max_subscribers: int = MAX_SUBSCRIBERS, max_history: int = MAX_HISTORY):
        if max_subscribers < 1:
            raise ValueError(f"max_subscribers must be positive, got {max_subscribers}")
        if max_history < 1:
            raise ValueError(f"max_history must be positive, got {max_history}")
        self._max_subscribers = max_subscribers
        self._listeners: list[TtsListener] = []
        self._history: deque[TtsEvent] = deque(maxlen=max_history)
        self._failures: deque[str] = deque(maxlen=MAX_FAILURES)

    @property
    def subscribers(self) -> int:
        return len(self._listeners)

    @property
    def history(self) -> tuple[TtsEvent, ...]:
        """The most recent events, oldest first, bounded by ``max_history``."""
        return tuple(self._history)

    @property
    def failures(self) -> tuple[str, ...]:
        """Listeners that raised, in the order they did, bounded."""
        return tuple(self._failures)

    def subscribe(self, listener: TtsListener) -> Callable[[], None]:
        """Register *listener* and return the call that removes it.

        Raises:
            TooManySubscribers: at the cap. Refusing is better than evicting an
                existing subscriber, which would silently stop delivering to a
                consumer that is still watching.
        """
        if len(self._listeners) >= self._max_subscribers:
            raise TooManySubscribers()
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def publish(self, event: TtsEvent) -> None:
        """Record *event* and hand it to every listener."""
        self._history.append(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:
                # A consumer's bug is not the speaker's problem. The listener is
                # dropped rather than retried, because a listener that raises
                # once on an event kind will raise on the next one too, and a
                # failing consumer that keeps being called is a loop.
                self._failures.append(f"{type(exc).__name__}: {exc}")
                if listener in self._listeners:
                    self._listeners.remove(listener)
