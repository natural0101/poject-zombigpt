"""The sidecar's view of recent world state: one snapshot plus a bounded ring.

Two rules from §3.4 and the "bounded everything" clause of AGENTS.md decide the
whole design:

* **A sequence gap is never interpolated.** If observation ``n+3`` arrives after
  ``n``, the store does not guess what happened in between and does not accept a
  partial update built against a baseline it does not have. It records the gap
  and asks for a full snapshot; a full snapshot re-synchronises, a partial one
  is refused.
* **History is a ring, not a log.** The store keeps a fixed number of recent
  observations so that reflex rules can look back a few ticks. At 4 Hz the
  default window is eight seconds of history, and it costs the same memory after
  a minute as after a week.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import islice
from typing import Final

from ..protocol import Observation

#: Recent observations kept for look-back. Eight seconds at the 4 Hz cadence of
#: §3.3 — long enough for stall detection, short enough to stay trivial.
DEFAULT_WINDOW: Final = 32

#: Hard ceiling on the ring, so a misconfigured window cannot become the
#: unbounded log this project forbids.
MAX_WINDOW: Final = 256

#: A window of one would make :meth:`ObservationStore.previous` meaningless,
#: and every reflex rule compares two consecutive observations.
MIN_WINDOW: Final = 2


@dataclass(frozen=True, slots=True)
class SeqGap:
    """Observations that were produced but never seen."""

    expected: int
    received: int

    @property
    def missing(self) -> int:
        return max(0, self.received - self.expected)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What the store did with one observation, and what the caller must do.

    ``full_snapshot_required`` is the actionable half: it stays true until a
    ``full`` observation lands, which is the only thing that can restore a
    baseline the store can diff against.
    """

    accepted: bool
    detail: str
    gap: SeqGap | None = None
    full_snapshot_required: bool = False
    session_changed: bool = False


class ObservationStore:
    """Latest snapshot, bounded recent history, and an honest gap check."""

    def __init__(self, *, capacity: int = DEFAULT_WINDOW) -> None:
        if not MIN_WINDOW <= capacity <= MAX_WINDOW:
            raise ValueError(f"capacity must be within {MIN_WINDOW}..{MAX_WINDOW}, got {capacity}")
        self._ring: deque[Observation] = deque(maxlen=capacity)
        self._session_id: str | None = None
        self._needs_full = True
        self._last_gap: SeqGap | None = None
        self._rejected = 0

    @property
    def capacity(self) -> int:
        return self._ring.maxlen or 0

    @property
    def size(self) -> int:
        return len(self._ring)

    @property
    def needs_full_snapshot(self) -> bool:
        """True while the store has no baseline it is willing to build on."""
        return self._needs_full

    @property
    def last_gap(self) -> SeqGap | None:
        """The most recent detected gap, kept for diagnostics."""
        return self._last_gap

    @property
    def rejected_count(self) -> int:
        """How many observations were refused. A counter, never a list."""
        return self._rejected

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def latest(self) -> Observation | None:
        """The newest accepted observation, or None before the first one."""
        return self._ring[-1] if self._ring else None

    def previous(self) -> Observation | None:
        """The one before :meth:`latest`, or None when there is only one."""
        return self._ring[-2] if len(self._ring) >= MIN_WINDOW else None

    def window(self, n: int) -> tuple[Observation, ...]:
        """The *n* most recent observations, newest first.

        Asking for more than the ring holds returns everything it holds rather
        than raising: the caller wants recent history, not a promise about how
        much of it survived.
        """
        if n < 1:
            raise ValueError(f"window size must be positive, got {n}")
        return tuple(islice(reversed(self._ring), min(n, len(self._ring))))

    def push(self, observation: Observation) -> IngestResult:
        """Offer one observation to the store.

        Refusing is a normal outcome, not an error: a replayed, out-of-order or
        gap-following partial observation is exactly what must not become the
        snapshot every other subsystem reads.
        """
        session_changed = self._session_id is not None and (
            observation.session_id != self._session_id
        )
        if session_changed:
            # Refs are session-scoped (§3.7): keeping observations from the old
            # session would leave the store holding refs that now denote other
            # objects entirely.
            self._discard_history()
            if not observation.full:
                return self._reject(
                    "session changed; a full snapshot is required before any partial update",
                    full_snapshot_required=True,
                    session_changed=True,
                )
            self._accept(observation)
            return IngestResult(
                accepted=True,
                detail="session changed; re-synchronised from a full snapshot",
                session_changed=True,
            )

        last = self.latest()
        if last is None:
            if not observation.full:
                return self._reject(
                    "no baseline yet; a partial observation cannot be applied",
                    full_snapshot_required=True,
                )
            self._accept(observation)
            return IngestResult(accepted=True, detail="baseline established")

        if observation.seq <= last.seq:
            return self._reject(
                f"observation seq {observation.seq} is not newer than {last.seq}",
                full_snapshot_required=self._needs_full,
            )

        expected = last.seq + 1
        if observation.seq == expected:
            self._accept(observation)
            return IngestResult(accepted=True, detail="in sequence")

        gap = SeqGap(expected=expected, received=observation.seq)
        self._last_gap = gap
        if not observation.full:
            return self._reject(
                f"{gap.missing} observation(s) missing; requesting a full snapshot",
                gap=gap,
                full_snapshot_required=True,
            )
        self._accept(observation)
        return IngestResult(
            accepted=True,
            detail=f"{gap.missing} observation(s) missing; re-synchronised from a full snapshot",
            gap=gap,
        )

    def reset(self) -> None:
        """Forget everything and require a fresh full snapshot."""
        self._discard_history()

    def _accept(self, observation: Observation) -> None:
        self._ring.append(observation)
        self._session_id = observation.session_id
        self._needs_full = False

    def _reject(
        self,
        detail: str,
        *,
        gap: SeqGap | None = None,
        full_snapshot_required: bool = False,
        session_changed: bool = False,
    ) -> IngestResult:
        self._rejected += 1
        if full_snapshot_required:
            self._needs_full = True
        return IngestResult(
            accepted=False,
            detail=detail,
            gap=gap,
            full_snapshot_required=self._needs_full,
            session_changed=session_changed,
        )

    def _discard_history(self) -> None:
        self._ring.clear()
        self._session_id = None
        self._needs_full = True
