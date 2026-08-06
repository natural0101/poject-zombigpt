"""The bounded, deduplicating utterance queue.

This is the module that decides the companion is bearable to sit next to. A
voice agent wired naively speaks on every tick, every internal transfer and
every observation diff; the blueprint forbids that outright, and the mechanism
that makes the ban structural rather than aspirational is here: a message about
a subject that already has one pending does not *queue*, it *replaces*. By the
time the mouth is free, only the current truth about each subject is left.

Three collapses, kept distinct because they mean different things:

* identical — the same thing was already going to be said, so nothing changes;
* superseded — something newer about the same subject replaces the pending one
  in place, keeping its position so a status update cannot jump the line;
* stale — a message with a lower revision than the pending one about the same
  subject is dropped, because a slower path finishing later does not make its
  information newer.

The bound is on distinct subjects, and it is enforced by urgency: at the cap the
least urgent, oldest pending message is evicted, and a newcomer that is itself
less urgent than everything pending is refused instead. An utterance of kind
``STOP`` therefore cannot be evicted by anything.
"""

from __future__ import annotations

from typing import Final

from pz_agent_core.ipc.clocks import Clock

from .events import TtsEvent, TtsEventKind, TtsEventStream
from .messages import VoiceOutput

__all__ = ["DEFAULT_MAX_PENDING", "UtteranceQueue"]

#: Distinct subjects that may be pending at once. Small on purpose: by the time
#: four things are waiting to be said, the fifth is almost certainly stale.
DEFAULT_MAX_PENDING: Final = 4


class UtteranceQueue:
    """Pending speech, deduplicated by subject and ordered by urgency."""

    def __init__(
        self,
        *,
        clock: Clock,
        events: TtsEventStream | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        if max_pending < 1:
            raise ValueError(f"max_pending must be positive, got {max_pending}")
        self._clock = clock
        self._events = events
        self._max_pending = max_pending
        #: (arrival, utterance). The arrival number is the tie-break within one
        #: urgency rank and survives a supersede, so replacing a pending message
        #: does not move it ahead of messages that were waiting longer.
        self._pending: list[tuple[int, VoiceOutput]] = []
        self._arrivals = 0

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def pending(self) -> tuple[VoiceOutput, ...]:
        """Everything waiting, in the order it will be spoken."""
        return tuple(message for _, message in sorted(self._pending, key=self._order))

    def __len__(self) -> int:
        return len(self._pending)

    def push(self, message: VoiceOutput) -> TtsEventKind:
        """Offer *message*, and report what the queue did with it."""
        index = self._index_of_topic(message.topic)
        if index is not None:
            return self._merge(index, message)
        if len(self._pending) >= self._max_pending and not self._make_room(message):
            return self._publish(TtsEventKind.DROPPED, message)
        self._arrivals += 1
        self._pending.append((self._arrivals, message))
        return self._publish(TtsEventKind.QUEUED, message)

    def pop(self) -> VoiceOutput | None:
        """The most urgent pending utterance, or None when there is nothing to say."""
        if not self._pending:
            return None
        entry = min(self._pending, key=self._order)
        self._pending.remove(entry)
        return entry[1]

    def clear(self) -> int:
        """Discard everything pending and report how much was discarded.

        Used by the stop path, where nothing that was queued before the stop is
        still worth saying afterwards.
        """
        count = len(self._pending)
        self._pending.clear()
        if count and self._events is not None:
            self._events.publish(TtsEvent(kind=TtsEventKind.CLEARED, at_ms=self._clock()))
        return count

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _order(entry: tuple[int, VoiceOutput]) -> tuple[int, int]:
        arrival, message = entry
        return (message.kind.rank, arrival)

    @staticmethod
    def _eviction_order(entry: tuple[int, VoiceOutput]) -> tuple[int, int]:
        """Largest is evicted first: least urgent, and oldest among equals.

        The arrival number is negated so that ``max`` picks the *oldest* of the
        least urgent messages — the one whose information has had the longest
        to go stale — rather than the one that just arrived.
        """
        arrival, message = entry
        return (message.kind.rank, -arrival)

    def _index_of_topic(self, topic: str) -> int | None:
        for index, (_, message) in enumerate(self._pending):
            if message.topic == topic:
                return index
        return None

    def _merge(self, index: int, message: VoiceOutput) -> TtsEventKind:
        arrival, existing = self._pending[index]
        if message.repeats(existing):
            return self._publish(TtsEventKind.COLLAPSED, message)
        if message.revision < existing.revision:
            return self._publish(TtsEventKind.COLLAPSED, message)
        self._pending[index] = (arrival, message)
        return self._publish(TtsEventKind.SUPERSEDED, message)

    def _make_room(self, incoming: VoiceOutput) -> bool:
        """Evict for *incoming* if anything pending is less urgent than it.

        Returns False when the queue is full of messages at least as urgent,
        which is the case where the honest answer is that the newcomer does not
        get said — dropping an error to make room for a status line would be
        exactly backwards.
        """
        victim = max(self._pending, key=self._eviction_order)
        if victim[1].kind.rank <= incoming.kind.rank:
            return False
        self._pending.remove(victim)
        self._publish(TtsEventKind.DROPPED, victim[1])
        return True

    def _publish(self, kind: TtsEventKind, message: VoiceOutput) -> TtsEventKind:
        if self._events is not None:
            self._events.publish(TtsEvent(kind=kind, at_ms=self._clock(), utterance=message))
        return kind
