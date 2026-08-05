"""Making a retried tool call cost nothing, at the boundary that can see the retry.

The mod already deduplicates a *redelivered command* by idempotency key. That
does not cover the case this module exists for: a client whose connection
dropped mid-call retries the tool, and without a record here the boundary would
mint a second command id for the same intent. The mod would then answer the
second one from its cache while the client held two ids for one action and could
not tell which it was waiting on.

So the only state this layer keeps is call identity: key → what that key already
produced. It is not a domain fact and it is not a copy of anything the core
holds; it is the boundary's half of the guarantee whose other half lives in
``ipc/queue.py``.

The map is bounded and least-recently-used, because it is fed by a caller: an
unbounded key space is an unbounded cache.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from pz_agent_core.protocol import JsonDict, ReasonCode

from .envelope import ToolFailure

__all__ = ["DEFAULT_CAPACITY", "MAX_CAPACITY", "CachedCall", "IdempotencyCache"]

DEFAULT_CAPACITY: Final = 128

#: A ceiling on the ceiling: a misconfigured capacity must not become the
#: unbounded map this project forbids.
MAX_CAPACITY: Final = 1_024


@dataclass(frozen=True, slots=True)
class CachedCall:
    """What one idempotency key already produced.

    ``action_id`` is set when the call put work in flight. A replay then re-reads
    that action's current state rather than returning the stored envelope, so a
    retry never resurrects a stale ``accepted`` for something that has since
    finished — and never invents a newer status for something that has not.

    ``status`` is the envelope status the first call answered with, kept for the
    replays that have no action to re-read: a plan submission, or an action the
    core has since forgotten. Without it a retry would report ``"ok"`` for a call
    that originally said ``accepted``, which is a different claim about the same
    work.
    """

    tool: str
    payload: JsonDict
    action_id: str | None = None
    status: str = "ok"


class IdempotencyCache:
    """Bounded, least-recently-used record of completed tool calls."""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError(f"capacity must be within 1..{MAX_CAPACITY}, got {capacity}")
        self._entries: OrderedDict[str, CachedCall] = OrderedDict()
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, tool: str, key: str) -> CachedCall | None:
        """The call *key* already produced for *tool*, or None.

        Raises:
            ToolFailure: ``INVALID_ARGUMENT`` when the key was used for a
                different tool. Answering with the other tool's result would be
                wrong and silently ignoring the collision would perform a second
                action under a key the client believes is spent, so the
                collision is reported as the client bug it is.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.tool != tool:
            raise ToolFailure(
                ReasonCode.INVALID_ARGUMENT,
                f"idempotency key was already used by {entry.tool}; a key names one call",
            )
        self._entries.move_to_end(key)
        return entry

    def remember(self, key: str, call: CachedCall) -> None:
        """Record *call* under *key*, evicting the least recently used entry."""
        self._entries[key] = call
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Forget every key.

        Called when the session changes: keys are session-scoped, and replaying
        a previous session's result would answer with an action taken in a world
        whose references no longer mean anything.
        """
        self._entries.clear()
