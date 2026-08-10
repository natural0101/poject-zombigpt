"""The deterministic route executor: the LLM picks the goal, this walks it.

A :class:`Journey` is created once per target and then fed the loop the caller
already runs — a fresh :class:`~pz_agent_core.protocol.Observation` into
:meth:`Journey.next_step`, each terminal
:class:`~pz_agent_core.protocol.ActionResult` into :meth:`Journey.note_result`
— and answers with exactly one of three frozen values: the next
:class:`~pz_agent_core.actions.ActionRequest` the existing engine already
serves, an :class:`Arrived` proven by an observation, or a :class:`Refused`
naming what stopped it. No I/O, no clock, no model call: given the same map
and the same inputs it produces the same steps.

Doors are handled the way wave 1 built them. ``movement.move_to`` ships with
``allow_doors`` and the mod opens unlocked doors it meets mid-walk, so a route
through a *known closed* door is still just a move — the executor's own
``door.open`` is reserved for the retry path, after a walk failed with a
door-shaped failure and the map knows which closed door stood on the leg. A
door observed (or refused) *locked* is a :data:`ReasonCode.DOOR_LOCKED`
refusal when no route avoids it — the executor does not hunt keys — and
barricaded mirrors it as :data:`ReasonCode.DOOR_BARRICADED`.

Everything is bounded, and every exhausted bound is a typed refusal rather
than a loop: the A* search by :data:`MAX_EXPANDED_NODES`, the journey by
:data:`MAX_LEGS` move legs and :data:`MAX_REPLANS` replans, and stuck
detection by :data:`MAX_CONSECUTIVE_FAILURES` completed legs without the
Chebyshev distance to the target decreasing.

Arrival is decided only by an observation placing the character within the
target radius on the target floor. A succeeded move ack is a statement about
the queue; the position is the postcondition.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, StrEnum, unique
from typing import Final

from ..actions.adapters.movement import (
    DEFAULT_ARRIVAL_RADIUS,
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
    MOVE_RETRY_POLICY,
)
from ..actions.engine import ActionRequest
from ..protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Observation,
    Position,
    ReasonCode,
)
from .local_map import CellSnapshot, DoorKnowledge, GridSquare, LocalMap, square_of

__all__ = [
    "DOOR_STEP_COST",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_EXPANDED_NODES",
    "MAX_JOURNEY_ID_LEN",
    "MAX_LEGS",
    "MAX_REPLANS",
    "Arrived",
    "Journey",
    "JourneyLimits",
    "JourneyState",
    "NavigationError",
    "NavigationFailure",
    "NavigationTarget",
    "NextStep",
    "Refused",
    "Step",
]

#: A* expansion budget. The map holds at most 4096 cells and the search box is
#: their bounding box plus a one-cell margin, so a search that has expanded
#: four times the map without finding the target is circling, not converging.
MAX_EXPANDED_NODES: Final = 16_384

#: Move legs one journey may spend. At up to 30 squares a leg this covers far
#: more ground than the local map can remember; a journey needing more has
#: outrun what "local navigation" honestly means.
MAX_LEGS: Final = 64

#: Replans one journey may consume. Each failed walk, refused door or
#: newly-observed obstruction costs one; a route this contested needs the
#: planner, not another lap.
MAX_REPLANS: Final = 8

#: Completed legs without the Chebyshev distance to the target decreasing (and
#: equally, consecutive failed results) before the journey refuses as stuck.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Extra route cost for stepping through a known closed door: the walk stops
#: to open it, so a detour of up to this many squares is worth taking first.
DOOR_STEP_COST: Final = 2.0

#: ``journey_id`` feeds the idempotency key, whose total length the protocol
#: caps at 120; this leaves room for the ``nav:``/``:stepNNN`` framing.
MAX_JOURNEY_ID_LEN: Final = 64


@unique
class JourneyState(Enum):
    """Where one journey stands. ARRIVED and REFUSED are terminal."""

    PLANNING = "planning"
    MOVING = "moving"
    ARRIVED = "arrived"
    REFUSED = "refused"


@unique
class NavigationFailure(StrEnum):
    """Why a journey refused — the executor's own taxonomy.

    Each value maps to the protocol :class:`ReasonCode` where one fits
    (:attr:`NavigationError.reason_code`); the budget exhaustions map to none,
    because "my search budget ran out" is a statement about this executor, not
    about the world, and borrowing ``PATH_NOT_FOUND`` for it would claim a
    proof that was never made.
    """

    NO_ROUTE = "NO_ROUTE"
    SEARCH_BUDGET_EXHAUSTED = "SEARCH_BUDGET_EXHAUSTED"
    DOOR_LOCKED = "DOOR_LOCKED"
    DOOR_BARRICADED = "DOOR_BARRICADED"
    STUCK = "STUCK"
    LEG_BUDGET_EXHAUSTED = "LEG_BUDGET_EXHAUSTED"
    REPLAN_BUDGET_EXHAUSTED = "REPLAN_BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class NavigationError:
    """One typed refusal: what failed, and the protocol code where one fits."""

    failure: NavigationFailure
    message: str
    reason_code: ReasonCode | None = None
    square: GridSquare | None = None
    door_ref: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """Where the journey is going, and how close counts as arrived."""

    x: int
    y: int
    z: int
    radius: float = DEFAULT_ARRIVAL_RADIUS

    def __post_init__(self) -> None:
        if not 0.1 <= self.radius <= MAX_ARRIVAL_RADIUS:
            raise ValueError(f"radius must be within 0.1..{MAX_ARRIVAL_RADIUS}, got {self.radius}")

    @property
    def square(self) -> GridSquare:
        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class JourneyLimits:
    """Every bound one journey runs under. All of them are load-bearing."""

    max_legs: int = MAX_LEGS
    max_replans: int = MAX_REPLANS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_expanded_nodes: int = MAX_EXPANDED_NODES
    leg_distance: int = MAX_MOVE_DISTANCE_SQUARES

    def __post_init__(self) -> None:
        for name in ("max_legs", "max_replans", "max_consecutive_failures", "max_expanded_nodes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")
        if not 1 <= self.leg_distance <= MAX_MOVE_DISTANCE_SQUARES:
            # The mod refuses longer single moves outright; a larger value here
            # would only manufacture TARGET_OUT_OF_RANGE failures downstream.
            raise ValueError(
                f"leg_distance must be within 1..{MAX_MOVE_DISTANCE_SQUARES}, "
                f"got {self.leg_distance}"
            )


@dataclass(frozen=True, slots=True)
class Step:
    """The next action to run: a move leg, or a door.open on the retry path.

    ``route`` is the planned squares this step is meant to cover (start
    exclusive, leg target inclusive) — the executor's working, exposed so a
    caller can display or audit the plan without re-deriving it.
    """

    request: ActionRequest
    leg_target: GridSquare
    route: tuple[GridSquare, ...]
    door_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Arrived:
    """The observed postcondition: the character stands within the radius."""

    target: NavigationTarget
    position: Position
    observation_seq: int


@dataclass(frozen=True, slots=True)
class Refused:
    """A typed refusal; the journey is over."""

    error: NavigationError


NextStep = Step | Arrived | Refused


# ---------------------------------------------------------------------------
# route search
# ---------------------------------------------------------------------------

#: The eight planar neighbour offsets, in a fixed order for determinism.
_PLANAR_STEPS: Final = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


@dataclass(frozen=True, slots=True)
class _RouteFound:
    #: Squares from just after the start to the goal, inclusive.
    squares: tuple[GridSquare, ...]


@dataclass(frozen=True, slots=True)
class _RouteExhausted:
    expanded: int


@dataclass(frozen=True, slots=True)
class _NoRoute:
    pass


_SearchOutcome = _RouteFound | _RouteExhausted | _NoRoute


def _passable(cell: CellSnapshot, *, pass_sealed_doors: bool) -> bool:
    """Whether the planner may route through this square.

    Unknown squares are traversable-optimistic — the map has no reason to
    forbid them — while known obstructions and doors known locked or
    barricaded are walls. ``pass_sealed_doors`` is the diagnosis mode: the
    second search pass walks through sealed doors purely to *name* the one
    that blocks the only route, never to plan through it.
    """
    if cell.door is not None:
        return pass_sealed_doors or cell.door.passable_hint
    return not cell.obstructed


#: A diagonal step costs a hair over a straight one. Chebyshev-metric routes
#: come in bundles of equal length, and a tie broken on raw square ordering
#: zig-zags; the epsilon (an exact binary fraction, so the arithmetic stays
#: deterministic) makes the straightest member of the bundle strictly cheapest
#: while keeping the Chebyshev heuristic admissible.
_DIAGONAL_STEP_COST: Final = 1.0 + 2.0**-10


def _step_cost(current: GridSquare, neighbour: GridSquare, cell: CellSnapshot) -> float:
    """Cost of stepping onto a square, plus the door toll when a door stands
    there not yet observed open (the walk must stop and swing it)."""
    diagonal = neighbour[0] != current[0] and neighbour[1] != current[1]
    cost = _DIAGONAL_STEP_COST if diagonal else 1.0
    if cell.door is not None and cell.door.open is not True:
        cost += DOOR_STEP_COST
    return cost


def _heuristic(square: GridSquare, goal: GridSquare) -> float:
    """Chebyshev distance plus floor gap — admissible for unit diagonal steps."""
    return float(max(abs(square[0] - goal[0]), abs(square[1] - goal[1])) + abs(square[2] - goal[2]))


def _search_box(
    local_map: LocalMap, start: GridSquare, goal: GridSquare
) -> tuple[int, int, int, int]:
    """The (min_x, max_x, min_y, max_y) box the search may roam.

    The bounding box of everything the map knows, plus start, goal and a
    one-square margin. The margin ring is entirely unknown and therefore
    passable and connected, which is what makes frontier exhaustion an honest
    ``NO_ROUTE``: any square that can reach the ring can reach any other, so
    the search only exhausts when known obstructions seal the start or the
    goal completely.
    """
    xs = [start[0], goal[0]]
    ys = [start[1], goal[1]]
    for x, y, _ in local_map.known_squares():
        xs.append(x)
        ys.append(y)
    return (min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1)


def _neighbours(
    local_map: LocalMap, square: GridSquare, box: tuple[int, int, int, int]
) -> Iterator[GridSquare]:
    """Planar neighbours inside the box, plus remembered stairs links.

    A z change exists only where an observation put stairs: a stairs square
    at floor z links (x, y, z) with (x, y, z + 1), in both directions. No
    stairs remembered, no link — the map never fabricates one.
    """
    x, y, z = square
    min_x, max_x, min_y, max_y = box
    for dx, dy in _PLANAR_STEPS:
        nx, ny = x + dx, y + dy
        if min_x <= nx <= max_x and min_y <= ny <= max_y:
            yield (nx, ny, z)
    if local_map.cell(x, y, z).stairs:
        yield (x, y, z + 1)
    if local_map.cell(x, y, z - 1).stairs:
        yield (x, y, z - 1)


def _find_route(
    local_map: LocalMap,
    start: GridSquare,
    goal: GridSquare,
    *,
    max_expanded_nodes: int,
    pass_sealed_doors: bool = False,
) -> _SearchOutcome:
    """A* over the local map. Deterministic: ties break on the square itself."""
    if start == goal:
        return _RouteFound(squares=())
    box = _search_box(local_map, start, goal)
    frontier: list[tuple[float, float, GridSquare]] = [(_heuristic(start, goal), 0.0, start)]
    best_cost: dict[GridSquare, float] = {start: 0.0}
    came_from: dict[GridSquare, GridSquare] = {}
    expanded = 0
    while frontier:
        _, cost, current = heapq.heappop(frontier)
        if cost > best_cost.get(current, float("inf")):
            continue  # a stale queue entry; the square was reached cheaper
        if current == goal:
            path: list[GridSquare] = [current]
            while path[-1] != start:
                path.append(came_from[path[-1]])
            path.reverse()
            return _RouteFound(squares=tuple(path[1:]))
        expanded += 1
        if expanded > max_expanded_nodes:
            return _RouteExhausted(expanded=expanded)
        for neighbour in _neighbours(local_map, current, box):
            cell = local_map.cell(*neighbour)
            if not _passable(cell, pass_sealed_doors=pass_sealed_doors):
                continue
            next_cost = cost + _step_cost(current, neighbour, cell)
            if next_cost < best_cost.get(neighbour, float("inf")):
                best_cost[neighbour] = next_cost
                came_from[neighbour] = current
                heapq.heappush(
                    frontier, (next_cost + _heuristic(neighbour, goal), next_cost, neighbour)
                )
    return _NoRoute()


def _first_sealed_door(
    local_map: LocalMap, squares: tuple[GridSquare, ...]
) -> tuple[GridSquare, DoorKnowledge] | None:
    """The first locked/barricaded door on a diagnosis-mode route, if any."""
    for square in squares:
        door = local_map.cell(*square).door
        if door is not None and not door.passable_hint:
            return square, door
    return None


# ---------------------------------------------------------------------------
# the journey
# ---------------------------------------------------------------------------


class _Pending:
    """The step last handed out, until its terminal result arrives."""

    __slots__ = ("action", "door_ref", "route", "target")

    def __init__(
        self,
        action: ActionName,
        target: GridSquare,
        route: tuple[GridSquare, ...],
        door_ref: str | None,
    ) -> None:
        self.action = action
        self.target = target
        self.route = route
        self.door_ref = door_ref


class Journey:
    """One target, driven step by step against a shared :class:`LocalMap`.

    The journey owns no I/O and no clock: :meth:`next_step` folds the fresh
    observation into the map, decides, and returns a frozen value; the caller
    runs the returned request through the action engine and reports the
    terminal result back via :meth:`note_result`. Once ARRIVED or REFUSED the
    journey is finished and :meth:`next_step` replays the same terminal value.
    """

    def __init__(
        self,
        local_map: LocalMap,
        target: NavigationTarget,
        *,
        journey_id: str = "journey",
        limits: JourneyLimits | None = None,
    ) -> None:
        if not 1 <= len(journey_id) <= MAX_JOURNEY_ID_LEN:
            raise ValueError(f"journey_id must be 1..{MAX_JOURNEY_ID_LEN} characters")
        self._map = local_map
        self._target = target
        self._journey_id = journey_id
        self._limits = limits if limits is not None else JourneyLimits()
        self._state = JourneyState.PLANNING
        self._outcome: Arrived | Refused | None = None
        self._pending: _Pending | None = None
        self._steps_emitted = 0
        self._legs_used = 0
        self._replans_used = 0
        self._consecutive_failures = 0
        self._legs_without_progress = 0
        self._best_distance: int | None = None
        self._leg_completed = False
        self._door_retry_route: tuple[GridSquare, ...] | None = None
        self._last_obstacle: NavigationError | None = None

    # -- read-only state ----------------------------------------------------

    @property
    def state(self) -> JourneyState:
        return self._state

    @property
    def target(self) -> NavigationTarget:
        return self._target

    @property
    def legs_used(self) -> int:
        return self._legs_used

    @property
    def replans_used(self) -> int:
        return self._replans_used

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the journey's bookkeeping.

        Non-terminal acks and results for actions this journey did not ask
        for are ignored — the journey only ever has one step in flight, and
        a stray result is somebody else's.
        """
        if self._outcome is not None or not result.is_terminal:
            return
        pending = self._pending
        if pending is None or result.action != pending.action.value:
            return
        self._pending = None
        if pending.action is ActionName.MOVEMENT_MOVE_TO:
            self._leg_completed = True
        if result.status is ActionStatus.SUCCEEDED:
            self._consecutive_failures = 0
            return
        # Every terminal failure consumes a replan: whatever the code, the
        # plan the step came from did not survive contact with the world.
        self._replans_used += 1
        self._consecutive_failures += 1
        self._note_failure(pending, result)

    def _note_failure(self, pending: _Pending, result: ActionResult) -> None:
        door_ref = self._failed_door_ref(pending, result)
        if result.reason_code is ReasonCode.DOOR_LOCKED and door_ref is not None:
            self._map.learn_door_locked(door_ref)
        if result.reason_code is ReasonCode.DOOR_BARRICADED and door_ref is not None:
            self._map.learn_door_barricaded(door_ref)
        if pending.action is ActionName.MOVEMENT_MOVE_TO and result.reason_code in (
            ReasonCode.PATH_STUCK,
            ReasonCode.PATH_NOT_FOUND,
        ):
            # The walk met something the plan did not know about. The fresh
            # observation fed to the next next_step may have taught the map
            # which closed door stood on this leg — remember the leg so that
            # door can be opened explicitly before replanning around.
            self._door_retry_route = pending.route
        self._last_obstacle = NavigationError(
            failure=NavigationFailure.STUCK,
            message=(
                f"{pending.action.value} to {pending.target} failed with {result.reason_code.value}"
            ),
            reason_code=result.reason_code,
            square=pending.target,
            door_ref=door_ref,
        )

    @staticmethod
    def _failed_door_ref(pending: _Pending, result: ActionResult) -> str | None:
        """The door a failure names: the one commanded, or the mod's evidence."""
        if pending.door_ref is not None:
            return pending.door_ref
        evidence_ref = result.evidence.get("door_ref")
        return evidence_ref if isinstance(evidence_ref, str) else None

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextStep:
        """The next thing to do, from the newest observation.

        Order matters and is fixed: learn, then check arrival (an observed
        arrival beats every budget), then stuck detection and budgets, then
        the door retry, then a fresh plan.
        """
        if self._outcome is not None:
            return self._outcome
        self._map.observe(observation)
        position = observation.player.position
        here = square_of(position)

        arrived = self._check_arrived(observation, position)
        if arrived is not None:
            return arrived
        refused = self._check_stuck(here) or self._check_replan_budget()
        if refused is not None:
            return refused

        door_step = self._door_retry_step(observation)
        if door_step is not None:
            return door_step
        return self._plan_and_emit(observation, here)

    def _check_arrived(self, observation: Observation, position: Position) -> Arrived | None:
        target = self._target
        if position.z != target.z:
            return None
        # Euclidean on the plane, matching the move adapter's own arrival test.
        if ((position.x - target.x) ** 2 + (position.y - target.y) ** 2) > target.radius**2:
            return None
        arrived = Arrived(target=target, position=position, observation_seq=observation.seq)
        self._state = JourneyState.ARRIVED
        self._outcome = arrived
        return arrived

    def _check_stuck(self, here: GridSquare) -> Refused | None:
        distance = max(abs(here[0] - self._target.x), abs(here[1] - self._target.y))
        if self._best_distance is None:
            self._best_distance = distance
        if self._leg_completed:
            self._leg_completed = False
            if distance < self._best_distance:
                self._legs_without_progress = 0
            else:
                self._legs_without_progress += 1
        self._best_distance = min(self._best_distance, distance)
        limit = self._limits.max_consecutive_failures
        if self._legs_without_progress < limit and self._consecutive_failures < limit:
            return None
        last = self._last_obstacle
        return self._refuse(
            NavigationError(
                failure=NavigationFailure.STUCK,
                message=(
                    f"no net progress towards {self._target.square} across {limit} "
                    "completed legs; last obstacle: "
                    + (last.message if last is not None else "none recorded")
                ),
                reason_code=ReasonCode.PATH_STUCK,
                square=last.square if last is not None else here,
                door_ref=last.door_ref if last is not None else None,
            )
        )

    def _check_replan_budget(self) -> Refused | None:
        if self._replans_used <= self._limits.max_replans:
            return None
        return self._refuse(
            NavigationError(
                failure=NavigationFailure.REPLAN_BUDGET_EXHAUSTED,
                message=(
                    f"the journey exhausted its {self._limits.max_replans}-replan budget "
                    f"short of {self._target.square}"
                ),
            )
        )

    def _door_retry_step(self, observation: Observation) -> Step | None:
        """After a door-shaped walk failure: open the known closed door on the leg.

        Only a door the map now knows is closed and neither locked nor
        barricaded qualifies — a sealed door is for the replan (or refusal)
        path, and an unknown one gives the retry nothing to aim at.
        """
        route = self._door_retry_route
        self._door_retry_route = None
        if route is None:
            return None
        for square in route:
            door = self._map.cell(*square).door
            if door is None or door.open is not False or not door.passable_hint:
                continue
            self._steps_emitted += 1
            request = ActionRequest(
                action=ActionName.DOOR_OPEN,
                session_id=observation.session_id,
                idempotency_key=self._step_key(),
                args={"door_ref": door.ref},
            )
            self._pending = _Pending(ActionName.DOOR_OPEN, square, (square,), door.ref)
            self._state = JourneyState.MOVING
            return Step(request=request, leg_target=square, route=(square,), door_ref=door.ref)
        return None

    def _plan_and_emit(self, observation: Observation, here: GridSquare) -> NextStep:
        outcome = _find_route(
            self._map,
            here,
            self._target.square,
            max_expanded_nodes=self._limits.max_expanded_nodes,
        )
        if isinstance(outcome, _RouteExhausted):
            return self._refuse(
                NavigationError(
                    failure=NavigationFailure.SEARCH_BUDGET_EXHAUSTED,
                    message=(
                        "the route search exhausted its budget "
                        f"({self._limits.max_expanded_nodes} nodes) short of "
                        f"{self._target.square}"
                    ),
                )
            )
        if isinstance(outcome, _NoRoute):
            return self._refuse(self._diagnose_no_route(here))
        if self._legs_used >= self._limits.max_legs:
            return self._refuse(
                NavigationError(
                    failure=NavigationFailure.LEG_BUDGET_EXHAUSTED,
                    message=(
                        f"the journey exhausted its {self._limits.max_legs}-leg budget "
                        f"short of {self._target.square}"
                    ),
                )
            )
        return self._emit_leg(observation, here, outcome.squares)

    def _diagnose_no_route(self, here: GridSquare) -> NavigationError:
        """Name what seals the route: a locked door, planks, or plain walls.

        The second search pass treats sealed doors as passable purely to find
        which one the only route runs through; if even that finds nothing, the
        map's remembered obstructions seal the way and the honest answer is
        ``PATH_NOT_FOUND``.
        """
        diagnosis = _find_route(
            self._map,
            here,
            self._target.square,
            max_expanded_nodes=self._limits.max_expanded_nodes,
            pass_sealed_doors=True,
        )
        if isinstance(diagnosis, _RouteFound):
            sealed = _first_sealed_door(self._map, diagnosis.squares)
            if sealed is not None:
                square, door = sealed
                if door.barricaded is True:
                    return NavigationError(
                        failure=NavigationFailure.DOOR_BARRICADED,
                        message=(
                            f"every remembered route to {self._target.square} runs through "
                            f"door {door.ref} at {square}, which is barricaded"
                        ),
                        reason_code=ReasonCode.DOOR_BARRICADED,
                        square=square,
                        door_ref=door.ref,
                    )
                return NavigationError(
                    failure=NavigationFailure.DOOR_LOCKED,
                    message=(
                        f"every remembered route to {self._target.square} runs through "
                        f"door {door.ref} at {square}, which is locked; "
                        "this executor does not hunt keys"
                    ),
                    reason_code=ReasonCode.DOOR_LOCKED,
                    square=square,
                    door_ref=door.ref,
                )
        return NavigationError(
            failure=NavigationFailure.NO_ROUTE,
            message=(
                f"no route to {self._target.square} survives what the map remembers from {here}"
            ),
            reason_code=ReasonCode.PATH_NOT_FOUND,
            square=self._target.square,
        )

    def _emit_leg(
        self, observation: Observation, here: GridSquare, squares: tuple[GridSquare, ...]
    ) -> Step:
        route = self._cut_leg(here, squares)
        leg_target = route[-1]
        is_final = leg_target == self._target.square
        distance = max(1, abs(leg_target[0] - here[0]), abs(leg_target[1] - here[1]))
        self._steps_emitted += 1
        self._legs_used += 1
        request = ActionRequest(
            action=ActionName.MOVEMENT_MOVE_TO,
            session_id=observation.session_id,
            idempotency_key=self._step_key(),
            args={
                "target": {"x": leg_target[0], "y": leg_target[1], "z": leg_target[2]},
                "radius": self._target.radius if is_final else DEFAULT_ARRIVAL_RADIUS,
                "max_distance": distance,
                "allow_doors": True,
                "allow_stairs": True,
            },
            policy=MOVE_RETRY_POLICY,
        )
        self._pending = _Pending(ActionName.MOVEMENT_MOVE_TO, leg_target, route, None)
        self._state = JourneyState.MOVING
        return Step(request=request, leg_target=leg_target, route=route)

    def _cut_leg(self, here: GridSquare, squares: tuple[GridSquare, ...]) -> tuple[GridSquare, ...]:
        """The leg-sized prefix of the route.

        A leg stays on one floor and inside the leg distance; a floor change
        is always a one-step leg of its own, so the move the mod validates
        targets the square directly across the remembered stairs link. An
        empty route (start square equals goal square, arrival radius not yet
        met) degenerates to the target square itself.
        """
        if not squares:
            return (self._target.square,)
        if squares[0][2] != here[2]:
            return squares[:1]
        end = 1
        for index, square in enumerate(squares, start=1):
            if square[2] != here[2]:
                break
            if max(abs(square[0] - here[0]), abs(square[1] - here[1])) > self._limits.leg_distance:
                break
            end = index
        return squares[:end]

    def _step_key(self) -> str:
        """Deterministic per-step idempotency key: same inputs, same keys."""
        return f"nav:{self._journey_id}:step{self._steps_emitted}"

    def _refuse(self, error: NavigationError) -> Refused:
        refused = Refused(error=error)
        self._state = JourneyState.REFUSED
        self._outcome = refused
        return refused
