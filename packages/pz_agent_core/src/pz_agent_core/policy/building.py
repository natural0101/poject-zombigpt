"""Whether a permanent structure may stand on a square, decided the boring way.

Crafting spends two planks. Building spends the square, and it spends it for
good: there is no demolition action anywhere in this build, because taking down
what somebody put there is a different authority and this project does not have
it. Every wall this module waves through is a wall the agent cannot take back
down, and that one asymmetry is why the gates here are stricter than
:mod:`pz_agent_core.policy.crafting`'s — which is otherwise this module's
sibling, and whose shapes it reuses rather than re-invents.

The decision surface is one observation, as it is for crafting. There is no
build-recipe table in this package and there will not be one: what the policy
reads is the building readout the mod publishes per item, described in
:class:`StructureView`, and the squares the observer already describes in
``nearby``. Nothing here is invented, and nothing here is remembered — the
answer is a pure function of the snapshot it was handed.

Six gates, in a fixed order, and the order is load-bearing. A picture that
fails several of them always reports the same refusal, and it is the one that
matters most rather than whichever check happened to run first:

1. **Can the world be read at all?** No window is not "probably fine, build
   away": an unreadable map is precisely the case where a trapping wall is most
   likely, so it refuses with :attr:`BuildingRefusal.WOULD_TRAP_PLAYER` before
   any other question is asked. Crafting starts with knowledge because a recipe
   nobody knows is its most fundamental fact; building starts here because
   every gate below reads the window.
2. **Is the structure one the character can build?** ``known`` absent means the
   build did not report whether the character has learned it, which is not the
   same as having learned it — the tri-state discipline the door adapters
   established and the crafting policy restated.
3. **Can the target square be read?** A square the observer did not describe,
   one it described as unloaded, or one on another floor than the character is
   a square this side cannot reason about. That is a refusal, not a shrug.
4. **Is the square free?** Something already standing there — a wall, a door, a
   tree, a crate — refuses, naming it. The agent never clears a square.
5. **Would this seal the character in?** :func:`enclosure_after`, below, and
   the reason this module exists.
6. **Are the materials present, and are they the user's to spend?** Last,
   because "you cannot build here at all" outranks "you are two planks short",
   and because a reserved plank is a question the user can answer while a
   trapping wall is not.

Bounded like everything else: :data:`MAX_STRUCTURES_PER_ITEM` readout entries
are read off any one item, a window larger than :data:`MAX_WINDOW_SQUARES` is
refused rather than read in part, and the flood fill's visited set is capped at
:data:`MAX_FLOOD_SQUARES`, with the cap being a *refusal* — a search that ran
out of budget has proven nothing.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from ..protocol import (
    InventoryView,
    ItemView,
    JsonDict,
    NearbyObject,
    Observation,
    Position,
)
from .config import DEFAULT_POLICY_CONFIG, PolicyConfig

# Crafting's, imported rather than copied. The requirement list of a build and
# the requirement list of a craft are the same list on the wire, counted against
# the same inventory under the same reserve rule, so a second implementation
# here would be a second thing to keep in step — and the half that drifted would
# be the half that says a placement is affordable. ``_read_materials``,
# ``_read_tristate`` and ``_tally`` are private to that module only because it
# had no sibling until this wave; the four public names carry the reporting
# shapes so a shortfall reads identically whichever policy produced it.
from .crafting import (
    MAX_REPORTED_SHORTFALLS,
    MaterialNeed,
    MaterialTally,
    Shortfall,
    _read_materials,
    _read_tristate,
    _tally,
)

__all__ = [
    "BUILDING_KEY",
    "DOOR_OBJECT_KIND",
    "MAX_FLOOD_SQUARES",
    "MAX_STRUCTURES_PER_ITEM",
    "MAX_WINDOW_SQUARES",
    "SEMANTIC_BLOCKED",
    "SEMANTIC_CLOSED_WINDOW",
    "SEMANTIC_DROP",
    "SEMANTIC_LOADED",
    "SQUARE_OBJECT_KIND",
    "BuildingDecision",
    "BuildingRefusal",
    "BuildingVerdict",
    "EnclosureCheck",
    "ObservedWindow",
    "Square",
    "StructureView",
    "assess_build",
    "assess_placement",
    "enclosure_after",
    "observed_structures",
    "read_window",
    "square_of",
    "structure_named",
]

#: One square on the world grid: ``(x, y, z)`` with ``z`` the floor.
Square = tuple[int, int, int]

#: Where the building readout rides on an item, exactly as the crafting readout
#: rides under ``crafting``: :class:`~pz_agent_core.protocol.ItemView` types
#: ``food``, ``literature`` and ``fluid`` and nothing else, so a new domain
#: travels under :attr:`~pz_agent_core.protocol.ItemView.extra` until somebody
#: decides it has earned a field of its own on the wire.
BUILDING_KEY: Final = "building"

#: How many readout entries are read off a single item. Data the game produced,
#: so it is bounded on arrival rather than trusted to be short.
MAX_STRUCTURES_PER_ITEM: Final = 16

#: The largest observed window this check will read. Far past any radius the
#: observer uses (``world.inspect`` caps at a five-by-five block), and a window
#: bigger than this is refused rather than read in part: dropping squares from
#: a window can only *remove* walls from it, which would turn a sealed room
#: into an open one and a refusal into a wall.
MAX_WINDOW_SQUARES: Final = 1024

#: The cap on the flood fill's visited set. It cannot be reached while
#: :data:`MAX_WINDOW_SQUARES` holds — every visited square is a described one —
#: and it is here anyway so the loop is bounded by its own code and not by an
#: invariant two functions away. Reaching it is a refusal: a search that ran out
#: of budget has not found an exit, and "did not find" is never "there is one".
MAX_FLOOD_SQUARES: Final = 1024

# How the observer names what this module reads. These five tokens are the
# observation contract — ``ObserveModel.BASE_SEMANTICS`` and ``Observe.lua`` in
# the mod are their source — and :mod:`pz_agent_core.actions.adapters.movement`
# mirrors the same strings for the action layer. They are mirrored here rather
# than imported because :mod:`pz_agent_core.policy` sits below the action layer
# and importing upward would make a placement policy depend on the adapter
# package that depends on it. ``tests/unit/test_policy_building.py`` pins the
# two mirrors equal, so the drift a second copy usually invites is a test
# failure rather than a silently permissive check.
SQUARE_OBJECT_KIND: Final = "square"
SEMANTIC_LOADED: Final = "loaded"
SEMANTIC_BLOCKED: Final = "blocked"
SEMANTIC_CLOSED_WINDOW: Final = "closed_window"
SEMANTIC_DROP: Final = "drop"

#: The kind the observer gives a door, mirrored from
#: :data:`pz_agent_core.actions.adapters.doors.DOOR_OBJECT_KIND` for the reason
#: above. A door is the one thing standing on a square that a character can
#: still walk through, so it is the one kind the escape route is allowed to
#: count on — and only when the observer positively read it as neither locked
#: nor barricaded.
DOOR_OBJECT_KIND: Final = "door"

#: Semantics on a square that make it no part of an escape route. ``blocked``
#: is the observer's own assessment; ``drop`` is a fall and ``closed_window`` is
#: a climb, and ``movement.move_to`` already refuses to walk onto either, so
#: neither may stand in for the exit this check claims to have found.
_IMPASSABLE_SEMANTICS: Final = frozenset({SEMANTIC_BLOCKED, SEMANTIC_DROP, SEMANTIC_CLOSED_WINDOW})

#: The four cardinal steps. Diagonals are deliberately absent: whether a
#: character may cut a wall's corner is a question about the engine's collision
#: shape that this side cannot answer, and an escape route that only exists
#: diagonally is exactly the route this check must not promise.
_STEPS: Final = ((1, 0), (-1, 0), (0, 1), (0, -1))


class BuildingVerdict(StrEnum):
    """The two answers, closed. A placement may go ahead now, or it may not."""

    BUILD = "build"
    REFUSE = "refuse"


class BuildingRefusal(StrEnum):
    """Why a placement is refused.

    Six tokens, one per fact, shaped like
    :class:`~pz_agent_core.policy.crafting.CraftingRefusal`: they reach reports,
    goal detail lines and the adapters' evidence bags, so they are constants
    here and nowhere else. The adapters map each onto the protocol reason code
    that exists for it — ``RECIPE_UNKNOWN``, ``TARGET_NOT_LOADED``,
    ``SQUARE_OCCUPIED``, ``WOULD_TRAP_PLAYER``, ``RECIPE_MATERIALS_MISSING``
    and ``RESOURCE_RESERVED``.
    """

    STRUCTURE_UNKNOWN = "structure_unknown"
    SQUARE_UNREADABLE = "square_unreadable"
    SQUARE_OCCUPIED = "square_occupied"
    WOULD_TRAP_PLAYER = "would_trap_player"
    MATERIALS_MISSING = "materials_missing"
    MATERIALS_RESERVED = "materials_reserved"


def square_of(position: Position) -> Square:
    """The grid square a world position stands on.

    The same floor-toward-negative rule
    :func:`pz_agent_core.actions.adapters.common.square_of` uses, restated here
    for the layering reason the semantic tokens above are restated: a placement
    that rounded a coordinate differently from the adapter that sends it would
    check one square and build on another.
    """
    return math.floor(position.x), math.floor(position.y), position.z


@dataclass(frozen=True, slots=True)
class StructureView:
    """Typed reading of one entry of an item's building readout.

    The wire shape, which the mod's observer produces and this side never
    invents::

        item.extra["building"] = {
            "structure_count": 2,
            "known_structure_count": 1,
            "structures": [
                {
                    "name": "WoodenWall",
                    "display_name": "Wooden Wall",
                    "known": true,
                    "blocks_movement": true,
                    "materials": [{"full_type": "Base.Plank", "count": 3}]
                }
            ]
        }

    ``known`` and ``blocks_movement`` are tri-state, and neither absence is read
    as the convenient answer. An unreported ``known`` refuses as unknown. An
    unreported ``blocks_movement`` is treated as *blocking*, which is the whole
    of the doorway question: a build that says nothing about whether what it
    places can be walked through gets no credit for maybe being a door frame,
    because the cost of guessing "door" is a character sealed in a room and the
    cost of guessing "wall" is one refused command.
    """

    name: str
    display_name: str
    known: bool | None
    blocks_movement: bool | None
    materials: tuple[MaterialNeed, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StructureView | None:
        """One readout entry, or None when it cannot be read whole.

        All-or-nothing, like :meth:`~pz_agent_core.policy.crafting.RecipeView.from_payload`
        and for the same reason: a structure that cannot say what it is has no
        postcondition to prove, and one whose requirement list cannot be read
        whole would understate what the placement spends.
        """
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return None
        materials = _read_materials(payload.get("materials"))
        if materials is None:
            return None
        display = payload.get("display_name")
        return cls(
            name=name,
            display_name=display if isinstance(display, str) and display else name,
            known=_read_tristate(payload, "known"),
            blocks_movement=_read_tristate(payload, "blocks_movement"),
            materials=materials,
        )

    @property
    def is_known(self) -> bool:
        """True only when the build said so. Unreadable is not learned."""
        return self.known is True

    @property
    def blocks_when_placed(self) -> bool:
        """True unless the build positively said the placement can be walked through.

        A doorway and a window frame are placements a character passes; a wall
        is not, and neither is anything the build declined to describe. This is
        the only place the distinction is drawn, and it falls to "it blocks" on
        absence because that is the answer whose mistake costs a sentence.
        """
        return self.blocks_movement is not False

    def as_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "known": self.known,
            "blocks_movement": self.blocks_movement,
            "blocks_when_placed": self.blocks_when_placed,
            "materials": [{"full_type": m.full_type, "count": m.count} for m in self.materials],
        }


@dataclass(frozen=True, slots=True)
class ObservedWindow:
    """The squares one observation described on one floor, and what stands on them.

    A *window*, not a map: it holds what a single snapshot carried and nothing
    remembered, so a square the observer did not mention this tick is absent
    here even if it was described a second ago. That is deliberate. The
    enclosure check's whole claim is about what can be seen *now*, and a
    remembered wall that has since been knocked down would make the claim a
    guess dressed as an observation.
    """

    floor: int
    #: Described square → the semantics the observer gave it.
    semantics: dict[Square, frozenset[str]] = field(default_factory=dict)
    #: Described square → the non-square objects standing on it.
    objects: dict[Square, tuple[NearbyObject, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.semantics)

    def describes(self, square: Square) -> bool:
        """Whether the observer said anything about this square at all."""
        return square in self.semantics

    def standing_on(self, square: Square) -> tuple[NearbyObject, ...]:
        return self.objects.get(square, ())

    def is_loaded(self, square: Square) -> bool:
        return SEMANTIC_LOADED in self.semantics.get(square, frozenset())

    def blockers_on(self, square: Square) -> tuple[str, ...]:
        """What stands on this square, named, or an empty tuple when nothing does.

        Every object the observer put on the square counts, not only the ones
        carrying the ``obstacle`` semantic. The mod's semantics table names
        obstacles for doors, windows and trees, which leaves a counter, a crate
        and a corpse arriving with no passability fact at all — and a square
        this side cannot vouch for is a square a permanent structure must not
        be dropped onto and an escape route must not run through. A door is
        listed too: it is a thing standing there, so it occupies the square,
        even though :meth:`is_passable` will still walk through it.
        """
        return tuple(f"{seen.kind}:{seen.ref}" for seen in self.standing_on(square))

    def is_occupied(self, square: Square) -> bool:
        """Whether something already stands on this square.

        Either the observer assessed the square itself as blocked, or it put an
        object on it. Both mean the same thing to a placement: the square is
        somebody's already, and clearing it is not an authority this build has.
        """
        if SEMANTIC_BLOCKED in self.semantics.get(square, frozenset()):
            return True
        return bool(self.standing_on(square))

    def is_passable(self, square: Square) -> bool:
        """Whether a character could walk onto this square, as far as this can tell.

        Conservative in every direction. The square must be described, loaded,
        free of the three semantics movement itself refuses, and carry nothing
        but doors the observer positively read as neither locked nor
        barricaded. Everything unknown counts against passability, because this
        predicate is only ever used to *find an exit*, and an exit that turns
        out not to exist is the failure this module was written to prevent.
        """
        marks = self.semantics.get(square)
        if marks is None or SEMANTIC_LOADED not in marks or marks & _IMPASSABLE_SEMANTICS:
            return False
        return all(_is_walk_through(seen) for seen in self.standing_on(square))

    def is_frontier(self, square: Square) -> bool:
        """Whether this square touches the edge of what the observation described.

        The stopping condition of the fill, and the exact point where the check
        stops claiming things. A square with an undescribed neighbour is a
        square from which the character may be able to keep going; whether they
        actually can is beyond the window, and this module never pretends to
        know.
        """
        x, y, z = square
        return any(not self.describes((x + dx, y + dy, z)) for dx, dy in _STEPS)


def _is_walk_through(seen: NearbyObject) -> bool:
    """Whether a character could pass the object standing on a square.

    Doors only, and only when the observer *positively* read both of the states
    a walk cannot resolve by itself. That is deliberately stricter than
    :attr:`~pz_agent_core.navigation.local_map.DoorKnowledge.passable_hint`,
    which counts a door passable unless it was seen locked or barricaded: that
    hint decides where to walk, and a walk that meets a locked door finds out
    and replans. This decides whether to put up something nothing in the build
    can take back down, so an unread lock is not an unlocked lock — it is a
    door this side cannot promise opens, and a promise is exactly what the
    escape route is.
    """
    if seen.kind != DOOR_OBJECT_KIND:
        return False
    return seen.locked is False and seen.barricaded is False


@dataclass(frozen=True, slots=True)
class EnclosureCheck:
    """The bounded flood fill's answer, carrying its own bound.

    ``claim`` is a field of the answer rather than a remark in a docstring
    because the bound is the most misreadable thing in this module. A pass here
    does **not** say the character is free; it says that within the squares this
    observation described, the placement leaves a route from where they stand to
    the edge of what can be seen. A character already sealed in by something
    outside the window is a character this check cannot see and does not claim
    anything about. What it can prove — and does — is the narrow, useful thing:
    this placement does not take away the last exit in view.
    """

    passed: bool
    detail: str
    start: Square | None
    #: The square at the edge of the window that the fill reached, when it did.
    exit_square: Square | None
    window_squares: int
    visited: int
    #: True when the fill stopped because it hit :data:`MAX_FLOOD_SQUARES`.
    hit_bound: bool = False

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an enclosure check must say what it saw")
        if self.passed and self.exit_square is None:
            raise ValueError("a passing enclosure check names the exit it found")

    @property
    def claim(self) -> str:
        """What this answer asserts, in the words a report should quote."""
        if self.passed:
            return (
                "within the squares this observation described, a route remains from where "
                "the character stands to the edge of what can be seen; nothing is claimed "
                "about ground beyond that edge"
            )
        return (
            "within the squares this observation described, no route remains from where the "
            "character stands to the edge of what can be seen"
        )

    def as_dict(self) -> JsonDict:
        return {
            "passed": self.passed,
            "detail": self.detail,
            "claim": self.claim,
            "start": None if self.start is None else _square_dict(self.start),
            "exit_square": None if self.exit_square is None else _square_dict(self.exit_square),
            "window_squares": self.window_squares,
            "visited": self.visited,
            "hit_bound": self.hit_bound,
        }


@dataclass(frozen=True, slots=True)
class BuildingDecision:
    """The verdict plus the observed facts that produced it.

    Shaped like :class:`~pz_agent_core.policy.crafting.CraftingDecision`: a
    refusal has to be able to say why without recomputing anything, and the
    adapter's precondition failure reads its evidence straight off the decision
    it acted on.
    """

    verdict: BuildingVerdict
    refusal: BuildingRefusal | None
    detail: str
    #: The structure the decision is about, or None when nothing was observed
    #: under the requested name at all.
    structure: StructureView | None
    #: The square the decision is about, or None when it could not be read.
    square: Square | None
    tallies: tuple[MaterialTally, ...] = ()
    #: What was found standing on the target square, named, when that is why.
    occupied_by: tuple[str, ...] = ()
    #: The enclosure answer, present whenever the check actually ran.
    enclosure: EnclosureCheck | None = None

    def __post_init__(self) -> None:
        if (self.verdict is BuildingVerdict.REFUSE) != (self.refusal is not None):
            raise ValueError("a refusal carries its token and only a refusal does")
        if not self.detail.strip():
            raise ValueError("a decision must say what it saw")

    @property
    def refused(self) -> bool:
        return self.verdict is BuildingVerdict.REFUSE

    @property
    def shortfalls(self) -> tuple[Shortfall, ...]:
        """Every requirement line the observation does not cover, bounded."""
        short = [t.as_shortfall() for t in self.tallies if not t.satisfied]
        return tuple(short[:MAX_REPORTED_SHORTFALLS])

    @property
    def needs_travel(self) -> bool:
        """Whether a material line is only covered by counting a world container.

        Reported, never acted on. ``crafting.craft`` escalates its tier on this
        fact; ``building.build`` has nowhere to escalate *to*, because placing
        something permanent is P4 whatever the planks were sitting in.
        """
        return any(t.needs_travel for t in self.tallies)

    def explain(self) -> str:
        return self.detail

    def as_dict(self) -> JsonDict:
        return {
            "verdict": self.verdict.value,
            "refusal": None if self.refusal is None else self.refusal.value,
            "detail": self.detail,
            "structure": None if self.structure is None else self.structure.as_dict(),
            "square": None if self.square is None else _square_dict(self.square),
            "shortfalls": [s.as_dict() for s in self.shortfalls],
            "occupied_by": list(self.occupied_by),
            "needs_travel": self.needs_travel,
            "enclosure": None if self.enclosure is None else self.enclosure.as_dict(),
        }


def _square_dict(square: Square) -> JsonDict:
    return {"x": square[0], "y": square[1], "z": square[2]}


# --------------------------------------------------------------------------
# reading the observation
# --------------------------------------------------------------------------
# Everything below is total over the mod's untyped readout: it is data from the
# game, which the working agreement classes as untrusted, so a missing key, a
# null or a value of the wrong type drops the entry rather than raising.


def _structures_on(item: ItemView) -> tuple[StructureView, ...]:
    """The readable readout entries one item carries, bounded per item."""
    block = item.extra.get(BUILDING_KEY)
    if not isinstance(block, Mapping):
        return ()
    entries = block.get("structures")
    if not isinstance(entries, (list, tuple)):
        return ()
    found: list[StructureView] = []
    for entry in entries[:MAX_STRUCTURES_PER_ITEM]:
        if not isinstance(entry, Mapping):
            continue
        structure = StructureView.from_payload(entry)
        if structure is not None:
            found.append(structure)
    return tuple(found)


def observed_structures(inventory: InventoryView) -> tuple[StructureView, ...]:
    """Every readable structure the observation names, deduplicated, name-ordered.

    The same structure is reported by every material it consumes, so the first
    reading of a name wins and the rest are dropped; sorting by name afterwards
    is what makes the result independent of the order the mod happened to
    enumerate the inventory in.
    """
    seen: dict[str, StructureView] = {}
    for item in inventory.items:
        for structure in _structures_on(item):
            seen.setdefault(structure.name, structure)
    return tuple(sorted(seen.values(), key=lambda s: s.name))


def structure_named(inventory: InventoryView, name: str) -> StructureView | None:
    """The one structure the observation reports under this exact name."""
    return next((s for s in observed_structures(inventory) if s.name == name), None)


def read_window(observation: Observation, floor: int) -> ObservedWindow | None:
    """The squares the observation described on *floor*, or None when unreadable.

    None covers three cases and they are one refusal to the caller: the mod
    produced no ``nearby`` tier, it produced one that describes no square on
    this floor, or it described more squares than :data:`MAX_WINDOW_SQUARES`.
    The third is refused rather than truncated on purpose — a window read in
    part has lost walls, never gained them, so trimming it could only ever turn
    a sealed room into an open one.

    Objects on other floors are dropped with their squares: an obstacle a
    storey below neither blocks a step here nor occupies a square here.
    """
    nearby = observation.nearby
    if nearby is None:
        return None
    window = ObservedWindow(floor=floor)
    standing: dict[Square, list[NearbyObject]] = {}
    for seen in nearby.objects:
        if seen.position is None or seen.position.z != floor:
            continue
        square = square_of(seen.position)
        if seen.kind == SQUARE_OBJECT_KIND:
            # Last description of a square wins, which matters only for a mod
            # that reported one twice; the semantics are a set either way.
            window.semantics[square] = frozenset(seen.semantics)
            if len(window.semantics) > MAX_WINDOW_SQUARES:
                return None
            continue
        standing.setdefault(square, []).append(seen)
    if not window.semantics:
        return None
    for square, objects in standing.items():
        window.objects[square] = tuple(objects)
    return window


# --------------------------------------------------------------------------
# the check this module exists for
# --------------------------------------------------------------------------


def enclosure_after(
    observation: Observation,
    window: ObservedWindow,
    placement: Square,
    *,
    blocks: bool,
) -> EnclosureCheck:
    """Is there still a way out, with the proposed structure treated as a wall?

    A breadth-first fill over the described window from the square the
    character stands on, four-connected, with *placement* removed from the
    passable set when the structure blocks. It succeeds the moment it steps
    onto a square that touches the edge of the window, and it fails when the
    queue empties, when the character's own square cannot be located in the
    window, or when the visited set reaches :data:`MAX_FLOOD_SQUARES`.

    Every failure mode is a refusal, including the two that are really "the
    check could not run". That is the point: an unreadable map is where a
    trapping wall is most likely, not least, and a placement whose consequences
    cannot be computed is not a placement that has been shown to be safe.

    What a pass proves is written into :attr:`EnclosureCheck.claim` and is
    narrower than it looks. It cannot show that the character is not already
    enclosed by something beyond the window — that would take a map this side
    does not have. It shows that this placement does not remove the last exit
    it can see, which is the only claim an observation of a bounded window can
    honestly support.
    """
    start = square_of(observation.player.position)
    size = len(window)
    if start[2] != window.floor or not window.describes(start):
        return EnclosureCheck(
            passed=False,
            detail=(
                "the character's own square is not among the squares this observation "
                "described, so there is nowhere for the check to start"
            ),
            start=None,
            exit_square=None,
            window_squares=size,
            visited=0,
        )
    if blocks and placement == start:
        return EnclosureCheck(
            passed=False,
            detail="the character is standing on the square this would wall off",
            start=start,
            exit_square=None,
            window_squares=size,
            visited=1,
        )

    visited: set[Square] = {start}
    queue: deque[Square] = deque([start])
    while queue:
        current = queue.popleft()
        if window.is_frontier(current):
            return EnclosureCheck(
                passed=True,
                detail=(
                    f"a route remains from ({start[0]}, {start[1]}) to the edge of the "
                    f"observed window at ({current[0]}, {current[1]})"
                ),
                start=start,
                exit_square=current,
                window_squares=size,
                visited=len(visited),
            )
        x, y, z = current
        for dx, dy in _STEPS:
            step = (x + dx, y + dy, z)
            if step in visited:
                continue
            if blocks and step == placement:
                continue
            if not window.is_passable(step):
                continue
            if len(visited) >= MAX_FLOOD_SQUARES:
                return EnclosureCheck(
                    passed=False,
                    detail=(
                        f"the search for a way out reached its bound of {MAX_FLOOD_SQUARES} "
                        "squares without finding one, and a search that ran out of budget "
                        "has proven nothing"
                    ),
                    start=start,
                    exit_square=None,
                    window_squares=size,
                    visited=len(visited),
                    hit_bound=True,
                )
            visited.add(step)
            queue.append(step)
    return EnclosureCheck(
        passed=False,
        detail=(
            f"every square reachable from ({start[0]}, {start[1]}) is walled in once this "
            f"structure stands at ({placement[0]}, {placement[1]}): {len(visited)} squares "
            "searched, none of them touching open ground"
        ),
        start=start,
        exit_square=None,
        window_squares=size,
        visited=len(visited),
    )


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------


def assess_placement(
    observation: Observation,
    structure: StructureView,
    square: Square,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> BuildingDecision:
    """One structure, one square, one observation, one deterministic answer.

    Total: every input shape yields a decision, never an exception. The gate
    order is the module docstring's and it does not vary, so the same picture
    always produces the same refusal however many gates it fails.
    """
    window = read_window(observation, square[2])
    if window is None:
        return _refuse(
            BuildingRefusal.WOULD_TRAP_PLAYER,
            (
                "the mod described no window this side can read whole on floor "
                f"{square[2]}, so whether this structure would seal the character in "
                "could not be computed at all — and an unreadable map is where a "
                "trapping wall is most likely, not least"
            ),
            structure=structure,
            square=square,
        )

    if not structure.is_known:
        cause = (
            "the build did not report whether the character has learned it, and "
            "unreadable is never learned"
            if structure.known is None
            else "the character has not learned it"
        )
        return _refuse(
            BuildingRefusal.STRUCTURE_UNKNOWN,
            f"structure {structure.name}: {cause}",
            structure=structure,
            square=square,
        )

    if not window.describes(square):
        return _refuse(
            BuildingRefusal.SQUARE_UNREADABLE,
            (
                f"the observation describes no square at ({square[0]}, {square[1]}) on "
                f"floor {square[2]}, so nothing can be said about what stands there"
            ),
            structure=structure,
            square=square,
        )
    if not window.is_loaded(square):
        return _refuse(
            BuildingRefusal.SQUARE_UNREADABLE,
            (
                f"the square at ({square[0]}, {square[1]}) is described but not loaded, "
                "so what would stand there could not be observed afterwards either"
            ),
            structure=structure,
            square=square,
        )

    if window.is_occupied(square):
        standing = window.blockers_on(square)
        named = ", ".join(standing) if standing else "the observer calls the square blocked"
        return _refuse(
            BuildingRefusal.SQUARE_OCCUPIED,
            (
                f"something already stands at ({square[0]}, {square[1]}): {named}. "
                "Clearing a square is not something this agent does"
            ),
            structure=structure,
            square=square,
            occupied_by=standing,
        )

    enclosure = enclosure_after(observation, window, square, blocks=structure.blocks_when_placed)
    if not enclosure.passed:
        return _refuse(
            BuildingRefusal.WOULD_TRAP_PLAYER,
            (
                f"putting {structure.display_name} at ({square[0]}, {square[1]}) would seal "
                f"the character in: {enclosure.detail}. Nothing in this build can take a "
                "structure back down, so a wall that traps is a wall that stays"
            ),
            structure=structure,
            square=square,
            enclosure=enclosure,
        )

    inventory = observation.inventory
    if inventory is None:
        # No inventory tier is no materials and no proof. The adapters refuse
        # this earlier with CAPABILITY_UNAVAILABLE; the policy stays total
        # rather than assuming a caller reached it in the right order.
        return _refuse(
            BuildingRefusal.MATERIALS_MISSING,
            "the mod reported no inventory, so no material can be counted",
            structure=structure,
            square=square,
            enclosure=enclosure,
        )
    tallies = tuple(_tally(inventory, need, config) for need in structure.materials)
    short = [t for t in tallies if not t.satisfied]
    if short:
        listed = "; ".join(t.as_shortfall().describe() for t in short[:MAX_REPORTED_SHORTFALLS])
        if all(t.satisfied_with_reserves for t in short):
            return _refuse(
                BuildingRefusal.MATERIALS_RESERVED,
                (
                    f"structure {structure.name} is short only because you reserved what it "
                    f"needs: {listed}"
                ),
                structure=structure,
                square=square,
                tallies=tallies,
                enclosure=enclosure,
            )
        return _refuse(
            BuildingRefusal.MATERIALS_MISSING,
            f"structure {structure.name} is short of {listed}",
            structure=structure,
            square=square,
            tallies=tallies,
            enclosure=enclosure,
        )

    return BuildingDecision(
        verdict=BuildingVerdict.BUILD,
        refusal=None,
        detail=(
            f"structure {structure.name} is known, ({square[0]}, {square[1]}) is free, "
            f"{enclosure.claim}, and every material is held: {len(tallies)} requirement "
            "lines, one structure"
        ),
        structure=structure,
        square=square,
        tallies=tallies,
        enclosure=enclosure,
    )


def assess_build(
    observation: Observation,
    name: str,
    square: Square,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> BuildingDecision:
    """Resolve *name* against the observation, then assess it on *square*.

    The convenience half of :func:`assess_placement`, for callers holding a
    name rather than a readout. A name nothing observed participates in is
    ``STRUCTURE_UNKNOWN`` and not an argument error: this side cannot say the
    character knows how to build a thing no item on them mentions.
    """
    inventory = observation.inventory
    structure = None if inventory is None else structure_named(inventory, name)
    if structure is None:
        return _refuse(
            BuildingRefusal.STRUCTURE_UNKNOWN,
            f"nothing observed on the character participates in a structure called {name}",
            structure=None,
            square=square,
        )
    return assess_placement(observation, structure, square, config)


def _refuse(
    refusal: BuildingRefusal,
    detail: str,
    *,
    structure: StructureView | None,
    square: Square | None,
    tallies: Sequence[MaterialTally] = (),
    occupied_by: Sequence[str] = (),
    enclosure: EnclosureCheck | None = None,
) -> BuildingDecision:
    return BuildingDecision(
        verdict=BuildingVerdict.REFUSE,
        refusal=refusal,
        detail=detail,
        structure=structure,
        square=square,
        tallies=tuple(tallies),
        occupied_by=tuple(occupied_by),
        enclosure=enclosure,
    )
