"""A bounded memory of what past observations proved about nearby squares.

The map is the navigation executor's substitute for asking a language model
"what is around me" once per square: every observation that passes through it
leaves behind the facts the observer actually carried — the player's own square
is walkable because the player stood on it, a door is a door because the
observer called it one, a square is obstructed because an obstacle was reported
on it — and nothing else. The map never invents. A square nobody observed is
*unknown*, which the planner treats as traversable-optimistic, and unknown is a
different answer from "known blocked" and from "proven walkable" everywhere in
this package.

Two rules keep the memory honest over time:

* **Newer knowledge wins, older knowledge never regresses it.** Every cell
  carries the ``seq`` of the observation that last touched it, and a stale
  observation replayed out of order is ignored for cells that have seen a newer
  one. Within a door's tri-state fields, ``None`` ("the build exposed no
  reader") never overwrites an observed ``True``/``False`` — an absent reading
  is an absent reading, not a fact.

* **Bounded, with honest forgetting.** At most :data:`MAX_CELLS` cells are
  kept; past that the oldest-seen cells are evicted. Eviction is forgetting,
  not an error: a forgotten square goes back to *unknown*, exactly as if it had
  never been observed.

Threat knowledge follows the same two rules with one extra honesty clause.
Every position-bearing zombie sighting lands on its square as a
:class:`ThreatSighting` (a zombie reported without a position cannot be pinned
to a square and taints none), the store is bounded at
:data:`MAX_THREAT_CELLS` with the oldest-seq sightings evicted first, and a
sighting older than :data:`THREAT_DECAY_SEQS` observation seqs stops counting
entirely. The decay horizon is a stated guess either way: zombies move, so a
remembered sighting is stale the moment it is made, and the map errs toward
caution *within* the horizon (the square stays tainted although the zombie
has probably wandered) and forgets *beyond* it (the square goes clean
although a zombie may still stand there). Both errors are survivable because
routing reads threat cost as a preference, never as a wall.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from ..actions.adapters.doors import DOOR_OBJECT_KIND
from ..actions.adapters.movement import (
    SEMANTIC_BLOCKED,
    SEMANTIC_STAIRS,
    SQUARE_OBJECT_KIND,
)
from ..protocol import NearbyObject, Observation, Position

__all__ = [
    "MAX_CELLS",
    "MAX_THREAT_CELLS",
    "SEMANTIC_OBSTACLE",
    "THREAT_DECAY_SEQS",
    "CellSnapshot",
    "DoorKnowledge",
    "GridSquare",
    "LocalMap",
    "ThreatSighting",
    "square_of",
]

#: One square on the world grid: (x, y, z) with z the floor.
GridSquare = tuple[int, int, int]

#: The cap on remembered squares. 4096 is a 64x64 neighbourhood — several times
#: the observer's own radius — so eviction only fires once a journey has walked
#: well past everything it can currently see.
MAX_CELLS: Final = 4096

#: The cap on remembered threat squares. Far below :data:`MAX_CELLS` on
#: purpose: a sighting decays within :data:`THREAT_DECAY_SEQS` observations,
#: so the live working set is what one horde can occupy inside the observation
#: radius, and 256 holds several hordes with room to spare. Past it the
#: oldest-seq sightings are evicted first — the ones closest to decaying
#: anyway, so eviction only ever hastens the forgetting already scheduled.
MAX_THREAT_CELLS: Final = 256

#: Observation seqs after which a sighting stops tainting its square. The
#: number is a horizon, not a fact about zombies: within it the map treats the
#: square as still dangerous (caution), beyond it as clean (zombies move).
#: Neither answer is knowledge — the honesty is in decaying at all rather
#: than remembering a zombie forever where it once stood.
THREAT_DECAY_SEQS: Final = 20

#: The semantic ``ObserveModel.BASE_SEMANTICS`` attaches to things a character
#: cannot walk through (doors, windows, trees). Mirrored here because the
#: Python side defines no constant for it; the Lua table is the source.
SEMANTIC_OBSTACLE: Final = "obstacle"

#: ``last_seen_seq`` of a cell nothing has ever observed. Observation sequence
#: numbers are non-negative, so the value can never collide with a real one.
NEVER_SEEN: Final = -1


def square_of(position: Position) -> GridSquare:
    """The grid square a world position stands on."""
    return math.floor(position.x), math.floor(position.y), position.z


@dataclass(frozen=True, slots=True)
class DoorKnowledge:
    """The last-seen state of one door, as the observer reported it.

    The three state fields keep the protocol's tri-state: ``None`` means no
    observation (and no mod refusal) has ever carried that fact, which is not
    the same as either answer.
    """

    ref: str
    open: bool | None
    locked: bool | None
    barricaded: bool | None
    orientation: str | None
    last_seen_seq: int

    @property
    def passable_hint(self) -> bool:
        """False only for a door observed locked or barricaded.

        A closed door is what doors are for — ``movement.move_to`` opens it on
        the way when ``allow_doors`` is set — so only the two states the walk
        cannot resolve by itself count as impassable here.
        """
        return self.locked is not True and self.barricaded is not True


@dataclass(frozen=True, slots=True)
class ThreatSighting:
    """The last zombie sighting recorded on one square.

    ``chasing`` is the sighting's own fact, not a running maximum: the newest
    observation of the square decides it, because a zombie that stopped
    chasing is exactly the kind of newer knowledge that must win. Whether the
    sighting still *counts* is the map's answer (:meth:`LocalMap.threat_at`
    applies the decay horizon); the record itself only says what was seen and
    when.
    """

    x: int
    y: int
    z: int
    chasing: bool
    last_seen_seq: int

    @property
    def square(self) -> GridSquare:
        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class CellSnapshot:
    """A frozen answer to "what does the map know about this square?".

    ``known`` is the unknown/known distinction: a snapshot with ``known=False``
    carries no facts at all, and callers must treat it as optimistic guesswork,
    never as proof. ``walkable_hint`` is only True for a square the character
    was actually observed standing on that no newer observation has obstructed
    — an unknown square is *plannable*, but it is never *proven* walkable.
    """

    x: int
    y: int
    z: int
    known: bool
    obstructed: bool
    visited: bool
    stairs: bool
    door: DoorKnowledge | None
    last_seen_seq: int

    @property
    def square(self) -> GridSquare:
        return (self.x, self.y, self.z)

    @property
    def walkable_hint(self) -> bool:
        return self.visited and not self.obstructed


class _CellState:
    """The mutable, internal form of one remembered cell."""

    __slots__ = ("door", "last_seen_seq", "obstructed", "stairs", "visited")

    def __init__(self, last_seen_seq: int) -> None:
        self.last_seen_seq = last_seen_seq
        self.obstructed = False
        self.visited = False
        self.stairs = False
        self.door: DoorKnowledge | None = None


class _Contribution:
    """Everything one observation said about one cell, before merging.

    Collected first and merged second so the outcome does not depend on the
    order objects happened to appear in ``nearby.objects``: the precedence is
    fixed here (square assessment, then obstacle presence, then the player's
    own standing proof) instead of being whatever came last in the list.
    """

    __slots__ = ("door", "obstacle", "player", "square_blocked", "stairs")

    def __init__(self) -> None:
        self.square_blocked: bool | None = None
        self.obstacle = False
        self.stairs = False
        self.door: NearbyObject | None = None
        self.player = False


def _merge_door(existing: DoorKnowledge | None, seen: NearbyObject, seq: int) -> DoorKnowledge:
    """Newest observed value per field; ``None`` never erases a known one.

    An observation that could not read the lock says nothing about the lock,
    so a door learned locked (from a mod ``DOOR_LOCKED`` refusal, say) stays
    known-locked until something positively reads the state — including
    positively reading it ``False`` after the player turned the key.
    """
    if existing is None:
        return DoorKnowledge(
            ref=seen.ref,
            open=seen.open,
            locked=seen.locked,
            barricaded=seen.barricaded,
            orientation=seen.orientation,
            last_seen_seq=seq,
        )
    return DoorKnowledge(
        ref=seen.ref,
        open=seen.open if seen.open is not None else existing.open,
        locked=seen.locked if seen.locked is not None else existing.locked,
        barricaded=seen.barricaded if seen.barricaded is not None else existing.barricaded,
        orientation=seen.orientation if seen.orientation is not None else existing.orientation,
        last_seen_seq=max(existing.last_seen_seq, seq),
    )


class LocalMap:
    """The bounded store of observed squares near the character."""

    def __init__(
        self, *, max_cells: int = MAX_CELLS, max_threat_cells: int = MAX_THREAT_CELLS
    ) -> None:
        if max_cells < 1:
            raise ValueError(f"max_cells must be at least 1, got {max_cells}")
        if max_threat_cells < 1:
            raise ValueError(f"max_threat_cells must be at least 1, got {max_threat_cells}")
        self._max_cells = max_cells
        self._max_threat_cells = max_threat_cells
        self._cells: dict[GridSquare, _CellState] = {}
        self._threats: dict[GridSquare, ThreatSighting] = {}
        self._revision = NEVER_SEEN

    @property
    def revision(self) -> int:
        """The highest observation ``seq`` ever ingested; -1 before the first."""
        return self._revision

    def __len__(self) -> int:
        return len(self._cells)

    # -- ingest -------------------------------------------------------------

    def observe(self, observation: Observation) -> None:
        """Fold one observation's facts into the map.

        The player's own square is walkable and visited; every nearby object
        that carries a position contributes to its square. Objects reported
        *without* a position are skipped honestly — a fact that cannot be
        pinned to a square cannot land on one.
        """
        seq = observation.seq
        self._revision = max(self._revision, seq)

        contributions: dict[GridSquare, _Contribution] = {}

        def at(square: GridSquare) -> _Contribution:
            found = contributions.get(square)
            if found is None:
                found = _Contribution()
                contributions[square] = found
            return found

        at(square_of(observation.player.position)).player = True

        if observation.nearby is not None:
            for seen in observation.nearby.objects:
                if seen.position is None:
                    continue
                contribution = at(square_of(seen.position))
                if seen.kind == DOOR_OBJECT_KIND:
                    # Doors carry the "obstacle" semantic too, but a door is
                    # remembered as a door: its passability is its state's.
                    if contribution.door is None:
                        contribution.door = seen
                    continue
                if seen.kind == SQUARE_OBJECT_KIND:
                    contribution.square_blocked = SEMANTIC_BLOCKED in seen.semantics
                if SEMANTIC_OBSTACLE in seen.semantics:
                    contribution.obstacle = True
                if SEMANTIC_STAIRS in seen.semantics:
                    contribution.stairs = True

        for square, contribution in contributions.items():
            self._apply(square, contribution, seq)
        self._evict_over_budget()
        self._observe_threats(observation, seq)

    def _observe_threats(self, observation: Observation, seq: int) -> None:
        """Record every position-bearing zombie sighting on its square.

        A zombie reported without a position is skipped honestly — the
        distance alone says "somewhere near", which lands on no square. Two
        zombies on one square in one observation merge with chasing OR-ed:
        one of them coming for the player makes the square a chasing square.
        An out-of-order replay never regresses a newer sighting, exactly the
        cell rule.
        """
        if observation.nearby is not None:
            for zombie in observation.nearby.zombies:
                if zombie.position is None:
                    continue
                square = square_of(zombie.position)
                known = self._threats.get(square)
                if known is not None and seq < known.last_seen_seq:
                    continue
                chasing = zombie.chasing
                if known is not None and known.last_seen_seq == seq:
                    chasing = chasing or known.chasing
                self._threats[square] = ThreatSighting(
                    x=square[0], y=square[1], z=square[2], chasing=chasing, last_seen_seq=seq
                )
        self._prune_threats()

    def _prune_threats(self) -> None:
        """Drop decayed sightings, then evict the oldest past the budget.

        Decay first: an entry past the horizon answers nothing anywhere, so
        keeping it would only let dead knowledge crowd live knowledge out of
        the budget. The eviction tie-break is the square itself, keeping
        forgetting deterministic like the cell store's.
        """
        horizon = self._revision - THREAT_DECAY_SEQS
        decayed = [sq for sq, seen in self._threats.items() if seen.last_seen_seq < horizon]
        for square in decayed:
            del self._threats[square]
        overflow = len(self._threats) - self._max_threat_cells
        if overflow <= 0:
            return
        doomed = sorted(self._threats, key=lambda sq: (self._threats[sq].last_seen_seq, sq))
        for square in doomed[:overflow]:
            del self._threats[square]

    def _apply(self, square: GridSquare, contribution: _Contribution, seq: int) -> None:
        cell = self._cells.get(square)
        if cell is None:
            cell = _CellState(seq)
            self._cells[square] = cell
        elif seq < cell.last_seen_seq:
            # An out-of-order replay: this cell already holds newer knowledge,
            # and older facts must not regress it.
            return
        cell.last_seen_seq = max(cell.last_seen_seq, seq)
        # Precedence, weakest to strongest: the observer's square assessment,
        # an obstacle standing on the square, the player standing on it.
        if contribution.square_blocked is not None:
            cell.obstructed = contribution.square_blocked
        if contribution.obstacle:
            cell.obstructed = True
        if contribution.stairs:
            cell.stairs = True
        if contribution.door is not None:
            cell.door = _merge_door(cell.door, contribution.door, seq)
        if contribution.player:
            cell.visited = True
            cell.obstructed = False

    def _evict_over_budget(self) -> None:
        overflow = len(self._cells) - self._max_cells
        if overflow <= 0:
            return
        # Honest forgetting: the cells longest unrefreshed go first, with the
        # coordinate tie-break keeping eviction deterministic.
        doomed = sorted(self._cells, key=lambda sq: (self._cells[sq].last_seen_seq, sq))[:overflow]
        for square in doomed:
            del self._cells[square]

    # -- results as evidence ------------------------------------------------

    def learn_door_locked(self, door_ref: str) -> bool:
        """Record a mod ``DOOR_LOCKED`` refusal against a remembered door.

        A refusal is evidence too — the mod read the lock off the engine
        object — but it names only a reference, so it can update a door the
        map already placed and nothing else. Returns False when no remembered
        door carries the reference; stamping is at the current revision so a
        later observation that positively reads the lock wins.
        """
        return self._learn_door_state(door_ref, locked=True)

    def learn_door_barricaded(self, door_ref: str) -> bool:
        """Record a mod ``DOOR_BARRICADED`` refusal; see :meth:`learn_door_locked`."""
        return self._learn_door_state(door_ref, barricaded=True)

    def _learn_door_state(
        self,
        door_ref: str,
        *,
        locked: bool | None = None,
        barricaded: bool | None = None,
    ) -> bool:
        for cell in self._cells.values():
            door = cell.door
            if door is None or door.ref != door_ref:
                continue
            cell.door = DoorKnowledge(
                ref=door.ref,
                open=door.open,
                locked=door.locked if locked is None else locked,
                barricaded=door.barricaded if barricaded is None else barricaded,
                orientation=door.orientation,
                last_seen_seq=max(door.last_seen_seq, self._revision),
            )
            return True
        return False

    # -- queries ------------------------------------------------------------

    def cell(self, x: int, y: int, z: int) -> CellSnapshot:
        """What the map knows about one square; ``known=False`` when nothing."""
        cell = self._cells.get((x, y, z))
        if cell is None:
            return CellSnapshot(
                x=x,
                y=y,
                z=z,
                known=False,
                obstructed=False,
                visited=False,
                stairs=False,
                door=None,
                last_seen_seq=NEVER_SEEN,
            )
        return CellSnapshot(
            x=x,
            y=y,
            z=z,
            known=True,
            obstructed=cell.obstructed,
            visited=cell.visited,
            stairs=cell.stairs,
            door=cell.door,
            last_seen_seq=cell.last_seen_seq,
        )

    def doors(self) -> Iterator[CellSnapshot]:
        """Every remembered door, as the snapshot of the square it stands on."""
        for (x, y, z), cell in self._cells.items():
            if cell.door is not None:
                yield self.cell(x, y, z)

    def known_squares(self) -> Iterator[GridSquare]:
        """Every remembered square, in insertion order (deterministic)."""
        yield from self._cells

    def threat_at(self, x: int, y: int, z: int) -> ThreatSighting | None:
        """The live sighting on one square, or None once it has decayed.

        The decay horizon is applied here rather than trusted to pruning, so
        an entry the last observation left one seq short of the horizon still
        answers honestly the moment a later revision passes it.
        """
        found = self._threats.get((x, y, z))
        if found is None or self._revision - found.last_seen_seq > THREAT_DECAY_SEQS:
            return None
        return found

    def threatened_cells(self) -> Iterator[ThreatSighting]:
        """Every live sighting, in insertion order (deterministic)."""
        for sighting in self._threats.values():
            if self._revision - sighting.last_seen_seq <= THREAT_DECAY_SEQS:
                yield sighting
