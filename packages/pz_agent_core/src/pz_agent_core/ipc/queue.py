"""The command stream: sequencing, idempotency, leases and backpressure.

This module is where "honest state" is mechanically enforced on the sidecar
side of the wire. Four rules from
``docs/blueprint/03_GAME_STATE_AND_IPC.md`` are implemented here, and each of
them exists because the obvious shortcut is dangerous:

* **§3.4 gap detection.** A missing sequence number is reported, never
  interpolated. The sidecar reacts by requesting a full snapshot, because the
  one thing it must not do is assume what happened in the gap.
* **§3.4 idempotency.** A redelivered command replays the *original* terminal
  result. Re-executing "eat half a sandwich" because an ack was lost costs the
  player food that cannot be un-eaten.
* **§3.9 leases.** Expiry is checked twice — when the command is queued and
  again immediately before it executes. The second check is the one that
  matters: a command can sit behind a long timed action and reach the front of
  the queue against a world that has moved on.
* **§3.11 backpressure.** At most one queue-occupying command is in flight.
  ``safety.stop`` bypasses all of it, because a stop that can be queued behind
  anything is not a stop.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from ..protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    READ_ONLY_ACTIONS,
    RETRYABLE_CODES,
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    JsonDict,
    ProtocolError,
    ReasonCode,
)
from .clocks import Clock, system_clock_ms
from .journal import (
    DEFAULT_KEEP,
    DEFAULT_MAX_BYTES,
    JournalDiagnostic,
    JournalReader,
    JournalWriter,
)
from .layout import IpcLayout

#: Commands that occupy the character's single action queue. Session control
#: and read-only probes are excluded: they complete without taking the queue,
#: so making them wait behind a long move would deadlock ordinary operation.
QUEUE_OCCUPYING_ACTIONS: Final[frozenset[ActionName]] = frozenset(
    action
    for action in ActionName
    if action not in READ_ONLY_ACTIONS
    and action not in ALWAYS_ALLOWED_ACTIONS
    and action is not ActionName.SESSION_ARM
)

DEFAULT_IDEMPOTENCY_CAPACITY: Final = 256
DEFAULT_GAP_HISTORY: Final = 32

#: How many shipped commands may await a terminal ack at once. Only one of them
#: can be queue-occupying (§3.11); the rest are session control and read-only
#: probes, so a small bound is enough and keeps the map from growing if the mod
#: stops acking altogether.
DEFAULT_PENDING_LIMIT: Final = 64


class Stream(StrEnum):
    """The four independently sequenced streams of §3.4."""

    OBSERVATION = "observation"
    COMMAND = "command"
    ACK = "ack"
    EVENT = "event"


class SequenceEvent(StrEnum):
    """What a received sequence number means relative to the last one."""

    IN_ORDER = "in_order"
    GAP = "gap"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class SequenceCheck:
    """The verdict on one received sequence number."""

    stream: Stream
    seq: int
    expected: int
    event: SequenceEvent

    @property
    def missing(self) -> int:
        """How many records the gap covers; zero unless this is a gap."""
        return max(0, self.seq - self.expected) if self.event is SequenceEvent.GAP else 0

    @property
    def needs_full_snapshot(self) -> bool:
        """A gap is only recoverable by resynchronising from a full snapshot."""
        return self.event is SequenceEvent.GAP


class SequenceTracker:
    """Monotonic sequence allocation and gap detection, per stream.

    Outbound and inbound use are separate: :meth:`allocate` hands out the next
    number for a stream this process produces, :meth:`observe` classifies a
    number that arrived from the peer.
    """

    def __init__(self, *, start: int = 0, gap_history: int = DEFAULT_GAP_HISTORY) -> None:
        if start < 0:
            raise ValueError(f"start must be non-negative, got {start}")
        if gap_history < 1:
            # A zero-length history is not "bounded", it is "keeps no evidence":
            # gaps would still be classified but nothing could be inspected
            # afterwards, which reads as a stream that never had a gap.
            raise ValueError(f"gap_history must be positive, got {gap_history}")
        self._next: dict[Stream, int] = {stream: start for stream in Stream}
        self._last_seen: dict[Stream, int | None] = {stream: None for stream in Stream}
        self._gaps: deque[SequenceCheck] = deque(maxlen=gap_history)

    def allocate(self, stream: Stream) -> int:
        """Return the next outbound sequence number for *stream*."""
        seq = self._next[stream]
        self._next[stream] = seq + 1
        return seq

    def peek(self, stream: Stream) -> int:
        return self._next[stream]

    def last_seen(self, stream: Stream) -> int | None:
        return self._last_seen[stream]

    def observe(self, stream: Stream, seq: int) -> SequenceCheck:
        """Classify an inbound sequence number and advance the expectation.

        On a gap the tracker jumps to *seq* so the same gap is reported once
        rather than on every subsequent record; the gap itself is kept in a
        bounded history for diagnostics.
        """
        if seq < 0:
            raise ValueError(f"seq must be non-negative, got {seq}")
        last = self._last_seen[stream]
        expected = 0 if last is None else last + 1
        if last is not None and seq <= last:
            return SequenceCheck(stream, seq, expected, SequenceEvent.DUPLICATE)
        event = SequenceEvent.IN_ORDER if seq == expected else SequenceEvent.GAP
        check = SequenceCheck(stream, seq, expected, event)
        self._last_seen[stream] = seq
        if event is SequenceEvent.GAP:
            self._gaps.append(check)
        return check

    @property
    def gaps(self) -> tuple[SequenceCheck, ...]:
        """Recent gaps, oldest first. Bounded; older ones are dropped."""
        return tuple(self._gaps)

    def reset(self, stream: Stream) -> None:
        """Forget what was seen on *stream* — used when the session generation
        changes and sequence numbers legitimately restart."""
        self._last_seen[stream] = None


class IdempotencyCache:
    """Bounded map from idempotency key to the command's terminal result.

    Eviction is by insertion order rather than by access: replaying a result
    must not extend the lifetime of a key, because that would let one busy key
    keep an unbounded tail of others alive.
    """

    def __init__(self, capacity: int = DEFAULT_IDEMPOTENCY_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._capacity = capacity
        self._entries: OrderedDict[str, ActionResult] = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def remember(self, key: str, result: ActionResult) -> None:
        """Cache a terminal result.

        Non-terminal acks (``received``/``started``/``progress``) are ignored:
        caching one would let a duplicate command be answered with a status
        that promises another ack which will never come for it.
        The first terminal result for a key wins — it is the one the peer has
        already been told about.
        """
        if not result.is_terminal or key in self._entries:
            return
        self._entries[key] = result
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def replay(self, key: str) -> ActionResult | None:
        """The cached terminal result for *key*, or None if it is not known."""
        return self._entries.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)


class LeaseCheckpoint(StrEnum):
    """Where a lease was checked, so a rejection can say which gate caught it."""

    ON_RECEIPT = "on_receipt"
    BEFORE_EXECUTION = "before_execution"


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """What happened to one :meth:`CommandQueue.submit` call.

    ``command`` is the command *as written*: when it was accepted it carries the
    sequence number the journal record actually got, which is not necessarily
    the provisional one :meth:`CommandQueue.build` handed out. Callers that keep
    a copy should keep this one.
    """

    command: Command
    accepted: bool
    offset: int | None = None
    replayed: ActionResult | None = None
    rejection: ActionResult | None = None

    @property
    def duplicate(self) -> bool:
        return self.replayed is not None

    @property
    def terminal_result(self) -> ActionResult | None:
        """The result the caller should report, if the command never shipped."""
        return self.replayed or self.rejection


@dataclass(frozen=True, slots=True)
class AckPoll:
    """One drain of the ack stream."""

    results: tuple[ActionResult, ...] = ()
    checks: tuple[SequenceCheck, ...] = ()
    diagnostics: tuple[JournalDiagnostic, ...] = ()
    rotated: bool = False
    lost_records: bool = False

    @property
    def gaps(self) -> tuple[SequenceCheck, ...]:
        return tuple(c for c in self.checks if c.event is SequenceEvent.GAP)

    @property
    def needs_full_snapshot(self) -> bool:
        return bool(self.gaps) or self.lost_records


@dataclass
class CommandQueue:
    """Writes the command stream and consumes the ack stream for one session.

    The queue owns exactly the state that has to survive between commands: the
    outbound sequence, the single in-flight slot, and the idempotency cache.
    Everything else — plan structure, retries, policy — belongs to layers above
    it.
    """

    layout: IpcLayout
    session_id: str
    clock: Clock = system_clock_ms
    idempotency_capacity: int = DEFAULT_IDEMPOTENCY_CAPACITY
    max_bytes: int = DEFAULT_MAX_BYTES
    keep: int = DEFAULT_KEEP
    pending_limit: int = DEFAULT_PENDING_LIMIT
    sequences: SequenceTracker = field(default_factory=SequenceTracker)
    _writer: JournalWriter = field(init=False)
    _acks: JournalReader = field(init=False)
    _cache: IdempotencyCache = field(init=False)
    _pending: OrderedDict[str, Command] = field(init=False)
    _in_flight: Command | None = field(default=None, init=False)
    _local_seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.pending_limit < 1:
            raise ValueError(f"pending_limit must be positive, got {self.pending_limit}")
        self.layout.ensure()
        self._writer = JournalWriter(
            self.layout,
            self.layout.command_queue,
            max_bytes=self.max_bytes,
            keep=self.keep,
            clock=self.clock,
        )
        self._acks = JournalReader(self.layout, self.layout.command_ack, keep=self.keep)
        self._cache = IdempotencyCache(self.idempotency_capacity)
        self._pending = OrderedDict()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._writer.close()

    @property
    def cache(self) -> IdempotencyCache:
        return self._cache

    @property
    def in_flight(self) -> Command | None:
        """The queue-occupying command awaiting a terminal ack, if any."""
        return self._in_flight

    @property
    def pending(self) -> tuple[Command, ...]:
        """Shipped commands that have not reached a terminal ack yet."""
        return tuple(self._pending.values())

    def command_reader_at_end(self) -> JournalReader:
        """A command-stream reader positioned past everything already written.

        §3.12: a restarted sidecar must not re-execute anything. A consumer
        with no persisted offset of its own starts here rather than at the top
        of the file, where the previous run's commands are still sitting. Acks
        are a different matter and are re-read from where they left off: they
        are how the sidecar learns what actually happened.
        """
        reader = JournalReader(self.layout, self.layout.command_queue, keep=self.keep)
        reader.seek_to_end()
        return reader

    # -- building and sending ---------------------------------------------

    def build(
        self,
        action: ActionName,
        *,
        idempotency_key: str,
        lease_ms: int,
        args: Mapping[str, object] | None = None,
        expected_observation_seq: int | None = None,
        policy: CommandPolicy | None = None,
        command_id: str | None = None,
    ) -> Command:
        """Compose a command and timestamp it.

        The sequence number here is *provisional*: it is the one the command
        will get if it is the next thing written. The definitive number is
        stamped by :meth:`submit` at the moment the record reaches the journal,
        because §3.4 makes a hole in a stream mean "a record was lost". A
        command that is rejected by a gate, or answered from the idempotency
        cache, is never written — and if it had already consumed a number, the
        mod would see a gap and conclude it had lost a command that was never
        sent.
        """
        payload: JsonDict = dict(args or {})
        return Command(
            session_id=self.session_id,
            seq=self.sequences.peek(Stream.COMMAND),
            command_id=command_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            issued_at_ms=self.clock(),
            lease_ms=lease_ms,
            action=action,
            args=payload,
            expected_observation_seq=expected_observation_seq,
            policy=policy,
        )

    def submit(self, command: Command) -> SubmitOutcome:
        """Run the admission gates and, if they all pass, write the command.

        Gate order is deliberate. The idempotency replay comes first: a
        duplicate of an already-finished command must answer with that
        command's original result, even if its lease has since expired — the
        work is done, and reporting ``LEASE_EXPIRED`` for it would be a lie.
        """
        now = self.clock()
        if command.session_id != self.session_id:
            return SubmitOutcome(
                command=command,
                accepted=False,
                rejection=self._reject(
                    command,
                    now,
                    ReasonCode.STALE_SESSION,
                    f"command belongs to session {command.session_id}",
                ),
            )

        replayed = self._cache.replay(command.idempotency_key)
        if replayed is not None:
            return SubmitOutcome(command=command, accepted=False, replayed=replayed)

        expired = self.check_lease(command, now, checkpoint=LeaseCheckpoint.ON_RECEIPT)
        if expired is not None:
            return SubmitOutcome(command=command, accepted=False, rejection=expired)

        occupies = command.action in QUEUE_OCCUPYING_ACTIONS
        if occupies and self._in_flight is not None:
            rejection = self._reject(
                command,
                now,
                ReasonCode.QUEUE_REJECTED,
                f"command {self._in_flight.command_id} is still in flight",
            )
            return SubmitOutcome(command=command, accepted=False, rejection=rejection)

        written, offset = self._write(command)
        if occupies:
            # Claimed before tracking, so the bound below already knows this one
            # must not be evicted.
            self._in_flight = written
        self._track(written)
        return SubmitOutcome(command=written, accepted=True, offset=offset)

    def _write(self, command: Command) -> tuple[Command, int]:
        """Stamp the definitive sequence number and append the record.

        Allocating here rather than in :meth:`build` is what keeps the command
        stream contiguous: only records that actually reach the journal consume
        a number.
        """
        stamped = replace(command, seq=self.sequences.allocate(Stream.COMMAND))
        return stamped, self._writer.append(stamped.to_dict())

    def _track(self, command: Command) -> None:
        """Remember a shipped command, oldest-first, within the bound.

        The in-flight command is never the one evicted. Dropping it would lose
        the idempotency key its terminal ack has to be filed under, and a
        redelivery of a *world-touching* command that this queue no longer
        recognises is exactly the double execution the cache exists to prevent.
        Read-only and session-control commands are cheaper to forget, so they
        are what the bound sheds.
        """
        self._pending[command.command_id] = command
        while len(self._pending) > self.pending_limit:
            victim = self._evictable_command_id()
            if victim is None:
                break
            self._pending.pop(victim, None)

    def _evictable_command_id(self) -> str | None:
        """The oldest pending command that is safe to forget, if there is one."""
        protected = {command.command_id for command in (self._in_flight,) if command is not None}
        for command_id in self._pending:
            if command_id not in protected:
                return command_id
        return None

    def send_stop(self, *, idempotency_key: str, lease_ms: int = 5_000) -> Command:
        """Write ``safety.stop`` past every gate. §3.11: stop bypasses the queue.

        No idempotency replay, no backpressure and no lease gate: each of those
        is a way for a stop to not happen, and a stop that does not happen is
        the worst failure this system has.
        """
        command = self.build(
            ActionName.SAFETY_STOP, idempotency_key=idempotency_key, lease_ms=lease_ms
        )
        written, _ = self._write(command)
        self._track(written)
        return written

    # -- leases ------------------------------------------------------------

    def check_lease(
        self,
        command: Command,
        now_ms: int | None = None,
        *,
        checkpoint: LeaseCheckpoint = LeaseCheckpoint.BEFORE_EXECUTION,
    ) -> ActionResult | None:
        """Return a ``LEASE_EXPIRED`` rejection when the lease has run out.

        Called once by :meth:`submit` and again by the executor immediately
        before the command runs.
        """
        moment = self.clock() if now_ms is None else now_ms
        if not command.is_expired(moment):
            return None
        overdue = moment - command.deadline_ms()
        return self._reject(
            command,
            moment,
            ReasonCode.LEASE_EXPIRED,
            f"lease expired {overdue} ms ago ({checkpoint.value})",
        )

    # -- acks --------------------------------------------------------------

    def poll_acks(self) -> AckPoll:
        """Drain new acks, applying sequencing, idempotency and backpressure."""
        read = self._acks.read()
        results: list[ActionResult] = []
        checks: list[SequenceCheck] = []
        diagnostics: list[JournalDiagnostic] = list(read.diagnostics)
        for record in read.records:
            try:
                result = ActionResult.from_dict(record.payload)
            except ProtocolError as exc:
                diagnostics.append(
                    JournalDiagnostic(offset=record.offset, detail=f"unusable ack: {exc}")
                )
                continue
            if result.session_id != self.session_id:
                diagnostics.append(
                    JournalDiagnostic(
                        offset=record.offset,
                        detail=f"ack for foreign session {result.session_id}; ignored",
                    )
                )
                continue
            check = self.sequences.observe(Stream.ACK, result.seq)
            checks.append(check)
            if check.event is SequenceEvent.DUPLICATE:
                # A redelivered ack carries no new information, but dropping it
                # silently would hide a peer that is resending; the check is
                # returned so the caller can log it.
                continue
            results.append(result)
            self.record_ack(result)
        return AckPoll(
            results=tuple(results),
            checks=tuple(checks),
            diagnostics=tuple(diagnostics),
            rotated=read.rotated,
            lost_records=read.lost_records,
        )

    def record_ack(self, result: ActionResult) -> None:
        """Apply one ack to the queue's state.

        A terminal result is cached under its command's idempotency key and,
        when that command held the backpressure slot, frees it. A non-terminal
        ack changes nothing: the command is still running, and caching a
        ``started`` would answer a duplicate with a status that promises an ack
        which will never arrive for it.
        """
        if not result.is_terminal:
            return
        command = self._pending.pop(result.command_id, None)
        if command is not None:
            self._cache.remember(command.idempotency_key, result)
        if self._in_flight is not None and self._in_flight.command_id == result.command_id:
            self._in_flight = None

    def close_in_flight(self, reason: ReasonCode, *, message: str = "") -> ActionResult | None:
        """Terminate the in-flight command as ``lost`` and return that result.

        §3.12: when the game restarts, the sidecar closes what was running as
        ``lost`` rather than guessing. ``lost`` is terminal, so it is cached —
        a redelivery of that command must not start the work again.
        """
        pending = self._in_flight
        if pending is None:
            return None
        result = ActionResult.failure(
            session_id=self.session_id,
            seq=self._next_local_seq(),
            command_id=pending.command_id,
            action=pending.action.value,
            timestamp_ms=self.clock(),
            reason_code=reason,
            status=ActionStatus.LOST,
            message=message or "in-flight command closed without an observed outcome",
        )
        self._cache.remember(pending.idempotency_key, result)
        self._pending.pop(pending.command_id, None)
        self._in_flight = None
        return result

    # -- helpers -----------------------------------------------------------

    def _next_local_seq(self) -> int:
        """Sequence numbers for results this process produced itself.

        They are counted apart from :class:`Stream.ACK`, which tracks what the
        mod published: a locally synthesised rejection never appears on that
        stream, so borrowing its numbering would corrupt gap detection.
        """
        seq = self._local_seq
        self._local_seq = seq + 1
        return seq

    def _reject(
        self, command: Command, now_ms: int, reason: ReasonCode, message: str
    ) -> ActionResult:
        """Build a locally produced terminal rejection.

        A rejection is cached only when the command can never become valid
        again. An expired lease or a foreign session id is permanent; a full
        queue is not, and caching ``QUEUE_REJECTED`` would make the retry the
        caller is supposed to perform replay the refusal forever.
        """
        result = ActionResult.failure(
            session_id=self.session_id,
            seq=self._next_local_seq(),
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=now_ms,
            reason_code=reason,
            status=ActionStatus.REJECTED,
            message=message,
        )
        if reason not in RETRYABLE_CODES:
            self._cache.remember(command.idempotency_key, result)
        return result
