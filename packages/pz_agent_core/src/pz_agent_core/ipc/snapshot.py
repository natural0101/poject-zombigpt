"""Full-snapshot exchange over two alternating slots and a pointer.

§3.5: an atomic rename cannot be assumed from inside Kahlua, so the full
snapshot is published by writing whichever slot file is *not* currently
pointed at, flushing it to disk, and only then rewriting the pointer. The
pointer is the commit point, and it is small enough that a reader either sees
the old slot name or the new one.

The reader holds the matching invariant: a snapshot is accepted only after the
entire document has been parsed. A slot caught mid-write fails that parse, and
the reader falls back to the other slot — which, by construction, still holds
the last snapshot that was successfully published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .atomic import DocumentError, SharingViolationError, read_json_document, write_json_atomic
from .clocks import Clock, system_clock_ms
from .layout import IpcLayout, SnapshotSlot

#: Pointer payload keys. The pointer is JSON so that a torn or truncated write
#: fails to parse rather than yielding a plausible-looking slot letter.
POINTER_SLOT: Final = "slot"
POINTER_SEQ: Final = "seq"
POINTER_WRITTEN_AT: Final = "written_at_ms"


@dataclass(frozen=True, slots=True)
class SnapshotRead:
    """A snapshot that parsed completely, plus how it was found."""

    slot: SnapshotSlot
    seq: int
    document: dict[str, Any]
    from_pointer: bool
    diagnostics: tuple[str, ...] = ()

    @property
    def recovered(self) -> bool:
        """True when the pointed-at slot was unusable and the other one served."""
        return not self.from_pointer


@dataclass(frozen=True, slots=True)
class SnapshotMiss:
    """No usable snapshot exists right now."""

    diagnostics: tuple[str, ...] = ()


class SnapshotWriter:
    """Publishes full snapshots by alternating slots.

    The next slot is chosen from the pointer on disk rather than from in-memory
    state, so a restarted writer never overwrites the slot a reader is about to
    follow.
    """

    def __init__(self, layout: IpcLayout, *, clock: Clock = system_clock_ms) -> None:
        self._layout = layout
        self._clock = clock

    @property
    def current_slot(self) -> SnapshotSlot | None:
        """The slot the pointer currently names, or None when it is unusable."""
        pointer = _read_pointer(self._layout.snapshot_pointer)
        return None if pointer is None else pointer[0]

    def next_slot(self) -> SnapshotSlot:
        current = self.current_slot
        return SnapshotSlot.A if current is None else current.other

    def publish(self, document: Mapping[str, Any]) -> SnapshotSlot:
        """Write *document* into the inactive slot and commit it.

        Requires an integer ``seq``: it is what lets a reader order two slots
        and detect that a fallback served an older snapshot.
        """
        seq = document.get(POINTER_SEQ)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise DocumentError("a snapshot document requires a non-negative integer 'seq'")
        slot = self.next_slot()
        write_json_atomic(self._layout, self._layout.snapshot_slot(slot), document)
        write_json_atomic(
            self._layout,
            self._layout.snapshot_pointer,
            {
                POINTER_SLOT: slot.value,
                POINTER_SEQ: seq,
                POINTER_WRITTEN_AT: self._clock(),
            },
        )
        return slot


@dataclass
class SnapshotReader:
    """Follows the pointer, validates the whole document, falls back on a tear.

    ``last_seq`` is remembered so a fallback cannot rewind the world model: a
    document older than one already accepted is reported and refused, because
    acting on stale inventory is worse than having no snapshot this tick.

    That comparison is *within one session*. §3.4 makes sequence numbers
    session-scoped and the mod restarts them from zero every time it accepts a
    new session, so the first snapshot of the next game — seq 0 — is numerically
    older than everything the previous session published. Ordering the two
    against each other refused the live world for as long as it took the new
    session to climb past the dead one's last number, which on the snapshot
    channel is the *only* observation channel there is: the sidecar sat blind
    with a diagnostic that read like corruption. A document from a different
    session is therefore not comparable, and re-anchors the reader instead.

    A file locked by another process (the Windows read-side contention
    :mod:`.atomic` reports as :class:`SharingViolationError`) is a third
    outcome, distinct from both a tear and a rewind: the document behind the
    lock is presumed good, so the poll reports a miss naming the lock and
    leaves ``last_seq`` untouched for the next poll to build on.

    ``session_id`` binds the reader to one session, and it is what a reader
    that has accepted nothing yet has instead of ``last_seq``. The exchange
    directory outlives the session that filled it: a sidecar that restarts, or
    attaches for the first time, into a directory still holding the previous
    session's slots follows the pointer to that session's last document, and
    with nothing accepted there is no number to order it against — it is served
    as the first observation of the new session, and
    :class:`~pz_agent_core.observation.store.ObservationStore`, whose session
    is unset until its first accept, takes it as the baseline. The rewind guard
    re-anchors on the mod's next publish, so this corrects itself; the first
    observation, which an attach and an arm decision are made against, does
    not. A bound reader refuses any document naming another session — or naming
    none, which cannot be shown to be this one's — with a diagnostic naming
    both sessions, so the caller reports that it has no observation and why.
    Bound by session rather than by age — the bound the arm gate applies to the
    same files — because the question the first observation asks is *whose*
    world this is rather than how old it is, and the handshook session id is
    the answer to it. An age bound would still admit a document from a session
    that ended a second ago; this one does not.

    ``None`` — the default — is no session filter at all, which is what every
    caller that publishes bare ``{"seq": …}`` documents and every reader that
    predates the binding gets.
    """

    layout: IpcLayout
    session_id: str | None = None
    _last_seq: int | None = field(default=None, init=False)
    _last_session: str | None = field(default=None, init=False)

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    def read(self) -> SnapshotRead | SnapshotMiss:
        diagnostics: list[str] = []
        try:
            pointer = _read_pointer(self.layout.snapshot_pointer)
        except SharingViolationError:
            # The pointer is held open by another process — on Windows that is
            # the game mid-commit, live-confirmed on Build 42.20.2. The
            # document behind the lock is fine, it just cannot be seen right
            # now, so this is a miss for this poll and nothing more: not
            # corruption, not a crash, and ``_last_seq`` stays where it is so
            # the next poll picks up exactly where this one could not.
            return SnapshotMiss(
                diagnostics=(*diagnostics, "pointer locked by another process this poll")
            )
        order: list[tuple[SnapshotSlot, bool]]
        if pointer is None:
            diagnostics.append("snapshot pointer is missing or unreadable; probing both slots")
            order = [(SnapshotSlot.A, False), (SnapshotSlot.B, False)]
        else:
            order = [(pointer[0], True), (pointer[0].other, False)]

        candidates: list[SnapshotRead] = []
        for slot, from_pointer in order:
            try:
                document = read_json_document(self.layout.snapshot_slot(slot))
            except SharingViolationError:
                # Same contention as the pointer, on a slot: the other slot may
                # still serve, so this is a per-slot miss, kept apart from the
                # corruption diagnostics because the document is not suspect.
                diagnostics.append(f"slot {slot.value} locked by another process this poll")
                continue
            except DocumentError as exc:
                diagnostics.append(f"slot {slot.value}: {exc}")
                continue
            seq = document.get(POINTER_SEQ)
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                diagnostics.append(f"slot {slot.value}: missing or invalid 'seq'")
                continue
            document_session = _document_session(document)
            if self.session_id is not None and document_session != self.session_id:
                # Refused here rather than after the two slots are ordered, so a
                # leftover document cannot win on its higher sequence number
                # either — the numbers of two sessions are not comparable.
                whose = (
                    f"holds session {document_session}"
                    if document_session is not None
                    else "names no session"
                )
                diagnostics.append(
                    f"slot {slot.value} {whose}, not the {self.session_id} this sidecar "
                    "handshook; it cannot be shown to describe this session's world, "
                    "so it is refused"
                )
                continue
            candidates.append(
                SnapshotRead(
                    slot=slot,
                    seq=seq,
                    document=document,
                    from_pointer=from_pointer,
                    diagnostics=(),
                )
            )
            if from_pointer:
                # The pointed-at slot parsed; no reason to read the other one.
                break

        if not candidates:
            return SnapshotMiss(diagnostics=tuple(diagnostics))

        chosen = max(candidates, key=lambda read: read.seq)
        session = _document_session(chosen.document)
        if session != self._last_session:
            # Numbers from two sessions cannot be ordered, so the rewind guard
            # has nothing to say here. Said out loud rather than passed over:
            # the world the caller is about to be handed belongs to a different
            # session than the one it was tracking.
            if self._last_session is not None:
                diagnostics.append(
                    f"slot {chosen.slot.value} holds session {session}, "
                    f"not the {self._last_session} seen so far; sequence numbers "
                    "restart per session, so this one is not a rewind"
                )
        elif self._last_seq is not None and chosen.seq < self._last_seq:
            diagnostics.append(
                f"slot {chosen.slot.value} holds seq {chosen.seq}, "
                f"older than the accepted {self._last_seq}; refused"
            )
            return SnapshotMiss(diagnostics=tuple(diagnostics))
        self._last_seq = chosen.seq
        self._last_session = session
        return SnapshotRead(
            slot=chosen.slot,
            seq=chosen.seq,
            document=chosen.document,
            from_pointer=chosen.from_pointer,
            diagnostics=tuple(diagnostics),
        )


def _document_session(document: Mapping[str, Any]) -> str | None:
    """The session a snapshot document names, or None when it names none.

    None is a value like any other here: a document without a session id is
    compared against other documents without one, which is what keeps the
    rewind guard working for the callers that publish bare ``{"seq": …}``
    documents.
    """
    session = document.get("session_id")
    return session if isinstance(session, str) else None


def _read_pointer(path: Path) -> tuple[SnapshotSlot, int] | None:
    """Parse the pointer file, or None when it cannot be trusted.

    Only :class:`DocumentError` becomes ``None`` — a missing or torn pointer
    genuinely cannot be trusted. :class:`SharingViolationError` propagates: a
    locked pointer is a perfectly good document behind a brief hold, and the
    two callers want different things from it — the reader turns it into a
    miss for this poll, while the writer lets it surface as the same typed
    contention refusal its own publish path already raises.
    """
    try:
        payload = read_json_document(path)
    except DocumentError:
        return None
    raw_slot = payload.get(POINTER_SLOT)
    if not isinstance(raw_slot, str):
        return None
    try:
        slot = SnapshotSlot(raw_slot)
    except ValueError:
        return None
    seq = payload.get(POINTER_SEQ)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None
    return slot, seq
