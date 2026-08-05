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

from .atomic import DocumentError, read_json_document, write_json_atomic
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
    """

    layout: IpcLayout
    _last_seq: int | None = field(default=None, init=False)

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    def read(self) -> SnapshotRead | SnapshotMiss:
        diagnostics: list[str] = []
        pointer = _read_pointer(self.layout.snapshot_pointer)
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
            except DocumentError as exc:
                diagnostics.append(f"slot {slot.value}: {exc}")
                continue
            seq = document.get(POINTER_SEQ)
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                diagnostics.append(f"slot {slot.value}: missing or invalid 'seq'")
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
        if self._last_seq is not None and chosen.seq < self._last_seq:
            diagnostics.append(
                f"slot {chosen.slot.value} holds seq {chosen.seq}, "
                f"older than the accepted {self._last_seq}; refused"
            )
            return SnapshotMiss(diagnostics=tuple(diagnostics))
        self._last_seq = chosen.seq
        return SnapshotRead(
            slot=chosen.slot,
            seq=chosen.seq,
            document=chosen.document,
            from_pointer=chosen.from_pointer,
            diagnostics=tuple(diagnostics),
        )


def _read_pointer(path: Path) -> tuple[SnapshotSlot, int] | None:
    """Parse the pointer file, or None when it cannot be trusted."""
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
