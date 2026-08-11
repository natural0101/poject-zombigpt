"""``explore_area``: a deterministic mission that walks the map's frontier.

The goal channel's fourth deterministic kind. Where ``loot_area`` owes every
reachable container a visit or a skip reason, ``explore_area`` owes every
*unknown square* in scope one of the same two answers: the mission reads the
shared :class:`~pz_agent_core.navigation.LocalMap`, finds the frontier —
unknown cells planar-adjacent to cells the map has proven walkable — walks a
:class:`~pz_agent_core.navigation.Journey` at the nearest one, lets the
observations that arrive on the way sweep cells into the map, and finishes on
one provable criterion: **no frontier cell remains in scope** — every scope
cell is either known to the map or carries a recorded skip reason (a locked
door named by reference, a proven ``NO_ROUTE``). No model call anywhere on
the path: the model (or ``pz_goal_submit``) said "explore"; every square
after that token is chosen here, deterministically.

The mission is a state machine over observations, shaped exactly like
:class:`~pz_agent_cli.loot_mission.LootMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` can drive both through one seam. It
answers with the *same* frozen move vocabulary — imported from
:mod:`pz_agent_cli.loot_mission` rather than restated, so the wrapper cannot
come to route the two missions' moves differently:

* :class:`~pz_agent_cli.loot_mission.MissionStep` travels the loop's **action
  channel** — every approach leg and door retry. No leg's success is the
  goal's postcondition (arriving at one waypoint is not "the area is
  explored"), so none of them may end the goal.
* :class:`~pz_agent_cli.loot_mission.MissionProbe` travels the **goal seam**,
  emitted only by a mission that finished without a single action running —
  the area was already known, which is exactly the "exploring your own room
  is a no-op" case the kind's ``radius`` default exists for. The probe is a
  bounded move inside the character's own square, whose *observed* success
  the loop settles the goal with.
* :class:`~pz_agent_cli.loot_mission.MissionComplete` and
  :class:`~pz_agent_cli.loot_mission.MissionRefused` end the goal through the
  queue — success on the last real channel result's evidence, refusal with a
  typed reason and a one-line report summary.

Honesty rules this module keeps, in the loot mission's own order:

* **Scope is pinned from an observation, never guessed.** ``scope=room`` or
  ``building`` with the character's own room unreadable is a typed
  ``PRECONDITION_FAILED`` naming ``scope=radius`` as the alternative. The
  map carries no room attribution, so for those two scopes the mission grows
  the scope's extent from what observations *prove* — the squares the
  character stood on and the squares of nearby objects whose reported room
  (or building) matches the pin — and the frontier is what borders that
  proven extent. Nothing outside the proof is swept.
* **Known means observed known.** A waypoint that becomes known while the
  journey is still walking at it is done without arrival — the point of the
  visit was the observation, and the observation happened.
* **Skips are first-class.** A door refused locked or barricaded — named by
  reference — and a frontier square the executor proved unroutable are each
  a recorded reason in the final report, never a silent drop.
* **Bounded everything.** Waypoints per mission, consecutive failures,
  completion probes, the proven-extent ledger, and every journey's own leg
  and replan budgets, all under the goal's wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals.model import AreaScope
from pz_agent_core.navigation import (
    MAX_JOURNEY_ID_LEN,
    Arrived,
    Journey,
    JourneyLimits,
    LocalMap,
    NavigationError,
    NavigationTarget,
    Refused,
    Step,
)
from pz_agent_core.navigation.local_map import GridSquare, square_of
from pz_agent_core.observation.compact import redact_text
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    JsonDict,
    Observation,
    ReasonCode,
)

from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    ENDED_UNPINNED,
    PHASE_APPROACH,
    PHASE_START,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "DEFAULT_EXPLORE_RADIUS",
    "EXPLORE_PHASES",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_SCOPE_EXTENT_CELLS",
    "MAX_WAYPOINTS",
    "WAYPOINT_ARRIVAL_RADIUS",
    "ExploreMission",
    "ExploreMissionLimits",
]

#: The closed phase set :attr:`ExploreMission.phase` reports, imported from
#: the loot mission's vocabulary rather than restated — the wrapper must not
#: come to read the two missions' phases differently. An explore mission has
#: no open/inspect/transfer: every waypoint is either being walked at
#: (``approach``) or the mission stands between waypoints (``start``).
EXPLORE_PHASES: Final = (PHASE_START, PHASE_APPROACH)


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Frontier waypoints one mission will ever walk at. Every waypoint costs at
#: least one bounded journey; a sweep that wants more than this is several
#: missions, stated as several goals, and a mission that hits the cap with
#: frontier remaining ends ``no_progress`` rather than claiming completion.
MAX_WAYPOINTS: Final = 32

#: Consecutive failures (stuck journeys, exhausted journey budgets — anything
#: that is not a typed world refusal like a locked door or a proven NO_ROUTE)
#: before the mission ends with ``NO_PROGRESS``. The loot mission's own
#: number, restated because the two missions are allowed to diverge and a
#: shared constant would hide the review when one of them does.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no action may spend, exactly the loot
#: mission's arrangement: each probe is one goal-seam request, so the goal's
#: own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: The Chebyshev sweep a ``scope=radius`` goal gets when it named none. The
#: loot mission's default, restated for the same divergence-review reason as
#: the failure bound above: a room-and-a-bit around where the user stood.
DEFAULT_EXPLORE_RADIUS: Final = 10

#: Squares the proven room/building extent may remember. Every entry is a
#: square an observation vouched for, and the map itself is bounded at
#: :data:`~pz_agent_core.navigation.MAX_CELLS`; this smaller ledger is what a
#: room or building scope can honestly claim, and past it the sweep stops
#: growing rather than growing without bound.
MAX_SCOPE_EXTENT_CELLS: Final = 1024

#: How close a journey must bring the character to a frontier waypoint. Wider
#: than a navigation goal's arrival radius on purpose: the waypoint square is
#: *unknown* — it may be a wall — and the visit's whole point is to bring it
#: inside the observation sweep, which standing beside it does just as well.
WAYPOINT_ARRIVAL_RADIUS: Final = 2.0

#: The eight planar neighbour offsets, in a fixed order for determinism —
#: the same order the route search walks them in.
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

#: The one skip reason the executor's proof produces verbatim, exported in
#: spirit via the report: a frontier square the search proved unreachable is
#: the "unreachable, with reason" half of the completion criterion.
_SKIP_NO_ROUTE: Final = "unreachable: NO_ROUTE"


def _door_named(reason: ReasonCode | None, door_ref: str | None) -> str | None:
    """A door-shaped skip reason, or None when the refusal is not one."""
    if reason is ReasonCode.DOOR_LOCKED:
        return f"door {door_ref or 'unknown'} is locked"
    if reason is ReasonCode.DOOR_BARRICADED:
        return f"door {door_ref or 'unknown'} is barricaded"
    return None


def _chebyshev(a: GridSquare, b: GridSquare) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExploreMissionLimits:
    """Every bound one mission runs under. All of them are load-bearing."""

    max_waypoints: int = MAX_WAYPOINTS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        for name in ("max_waypoints", "max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScopePin:
    """What "the area" resolved to, from the activation observation."""

    scope: AreaScope
    room: str = ""
    building: str = ""
    centre: GridSquare | None = None
    radius: int = 0


class ExploreMission:
    """One ``explore_area`` goal, driven frontier square by frontier square.

    Created by the wrapper when the goal activates and discarded when the
    goal reaches a terminal state — the mission never outlives its goal. All
    inputs are injected and deterministic: the shared
    :class:`~pz_agent_core.navigation.LocalMap` and the frozen limits.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        local_map: LocalMap,
        scope: AreaScope,
        radius: int = DEFAULT_EXPLORE_RADIUS,
        limits: ExploreMissionLimits | None = None,
        journey_limits: JourneyLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._key_stem = goal_id[:MAX_JOURNEY_ID_LEN]
        self._map = local_map
        self._scope = scope
        self._radius = radius
        self._limits = limits if limits is not None else ExploreMissionLimits()
        self._journey_limits = journey_limits if journey_limits is not None else JourneyLimits()

        self._pin: _ScopePin | None = None
        #: ``len(map)`` when the scope was pinned; the growth-delta base the
        #: report's ``cells_discovered`` is measured from.
        self._baseline_cells = 0
        #: Squares an observation proved inside a room/building pin. Unused
        #: for a radius pin, whose membership is arithmetic on the centre.
        self._extent: set[GridSquare] = set()
        #: Frontier squares already answered — visited, swept or skipped —
        #: so no square is walked at twice and every skip stays a skip.
        self._resolved: set[GridSquare] = set()

        self._waypoint: GridSquare | None = None
        self._journey: Journey | None = None
        self._pending_action: str | None = None
        self._waypoints_started = 0

        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False

        self._visited = 0
        self._skipped: list[tuple[GridSquare, str]] = []
        self._ended = ENDED_IN_PROGRESS
        self._final: MissionComplete | MissionRefused | None = None

    # -- read-only state ----------------------------------------------------

    @property
    def goal_id(self) -> str:
        return self._goal_id

    @property
    def ended(self) -> str:
        return self._ended

    @property
    def phase(self) -> str:
        """Where the mission stands: one of :data:`EXPLORE_PHASES`.

        ``approach`` while a journey is walking at a waypoint, ``start``
        between waypoints (which includes "not yet begun"). Closed tokens from
        the loot mission's own vocabulary, never caller or game text, so a
        status surface may publish the value verbatim; a mission that already
        ended keeps the phase it stopped in, because whether it ended is
        :attr:`ended`'s answer, not this one's.
        """
        return PHASE_APPROACH if self._waypoint is not None else PHASE_START

    @property
    def any_success(self) -> bool:
        """Whether any channel action this mission asked for succeeded."""
        return self._any_success

    def mark_abandoned(self) -> None:
        """Seal the report of a mission whose goal died under it.

        The wrapper's prune path. Only a mission still in flight is resealed —
        a mission that already ended keeps the ending it earned.
        """
        if self._ended is ENDED_IN_PROGRESS:
            self._ended = ENDED_CANCELLED

    @property
    def _discovered(self) -> int:
        """Map growth since the scope was pinned; zero before a pin exists."""
        if self._pin is None:
            return 0
        return max(0, len(self._map) - self._baseline_cells)

    @property
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        Room and building names came out of the game, so they pass through
        :func:`~pz_agent_core.observation.compact.redact_text` like every
        other game-authored string this project renders. Skips render in the
        order they were recorded, and a square is three numbers — nothing in
        this object carries free text.
        """
        scope: JsonDict = {"scope": self._scope.value}
        pin = self._pin
        if pin is not None:
            if pin.room:
                scope["room"] = redact_text(pin.room)
            if pin.building:
                scope["building"] = redact_text(pin.building)
            if pin.centre is not None:
                scope["centre"] = {"x": pin.centre[0], "y": pin.centre[1], "z": pin.centre[2]}
                scope["radius"] = pin.radius
        return {
            "scope": scope,
            "cells_discovered": self._discovered,
            "waypoints_visited": self._visited,
            "skipped": [
                {"square": {"x": square[0], "y": square[1], "z": square[2]}, "reason": reason}
                for square, reason in self._skipped
            ],
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line of the report, for a goal record's ``detail``.

        Constants and counts only — no refs, no game-authored names — so it
        satisfies the goal channel's rule that a detail is never caller text.
        """
        return (
            f"explore {self._ended}: discovered={self._discovered} "
            f"visited={self._visited} skipped={len(self._skipped)}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Every request this mission emits is a journey step, so every result
        is the journey's to learn from; the mission itself only counts the
        success that resets the failure streak. Non-terminal acks and results
        for actions this mission did not ask for are ignored — the mission
        has at most one step in flight.
        """
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        journey = self._journey
        if journey is not None:
            journey.note_result(result)
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True
            self._consecutive_failures = 0

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: learn the map, pin the scope, grow the proven extent,
        then drive the current waypoint — or pick the nearest frontier square
        — or finish. A finished mission replays its terminal value.
        """
        if self._final is not None:
            return self._final
        self._map.observe(observation)
        if self._pin is None:
            refused = self._pin_scope(observation)
            if refused is not None:
                return refused
        self._grow_extent(observation)
        if self._pending_action is not None:
            # A channel leg is still being driven; the map learned, the
            # mission decides on the observation after its result lands.
            return None
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive approaches failed",
            )
        # Bounded by the waypoint cap: each iteration either emits a request,
        # resolves the current waypoint (visited, swept or skipped — each of
        # which shrinks the remaining frontier), or finishes.
        while True:
            if self._waypoint is None:
                started = self._start_next_waypoint(observation)
                if isinstance(started, MissionRefused):
                    return started
                if not started:
                    return self._finish(observation)
            move = self._drive_waypoint(observation)
            if move is not None:
                return move

    # -- scope --------------------------------------------------------------

    def _pin_scope(self, observation: Observation) -> MissionRefused | None:
        player = observation.player
        if self._scope is AreaScope.ROOM:
            if player.room is None:
                return self._refuse(
                    ENDED_UNPINNED,
                    ReasonCode.PRECONDITION_FAILED,
                    "the build reports no room for the character here; use scope=radius",
                )
            self._pin = _ScopePin(
                scope=self._scope, room=player.room, building=player.building or ""
            )
        elif self._scope is AreaScope.BUILDING:
            if player.building is None:
                return self._refuse(
                    ENDED_UNPINNED,
                    ReasonCode.PRECONDITION_FAILED,
                    "the build reports no building for the character here; use scope=radius",
                )
            self._pin = _ScopePin(scope=self._scope, building=player.building)
        else:
            self._pin = _ScopePin(
                scope=self._scope,
                centre=square_of(player.position),
                radius=self._radius,
            )
        self._baseline_cells = len(self._map)
        return None

    def _grow_extent(self, observation: Observation) -> None:
        """Add every square this observation proves inside a room/building pin.

        The map has no room attribution, so a room or building scope's extent
        is exactly the set of squares some observation vouched for: the
        square the character stands on while the build reports the pinned
        room (or building), and the squares of nearby objects whose reported
        room (or building) matches. A radius pin needs none of this — its
        membership is arithmetic on the centre.
        """
        pin = self._pin
        if pin is None or pin.scope is AreaScope.RADIUS:
            return
        player = observation.player
        if pin.scope is AreaScope.ROOM:
            inside = player.room == pin.room and (
                not pin.building or player.building is None or player.building == pin.building
            )
        else:
            inside = player.building == pin.building
        if inside and len(self._extent) < MAX_SCOPE_EXTENT_CELLS:
            self._extent.add(square_of(player.position))
        nearby = observation.nearby
        if nearby is None:
            return
        for seen in nearby.objects:
            if len(self._extent) >= MAX_SCOPE_EXTENT_CELLS:
                return
            if pin.scope is AreaScope.ROOM:
                matches = seen.room is not None and seen.room == pin.room
            else:
                matches = seen.building is not None and seen.building == pin.building
            if matches and seen.position is not None:
                self._extent.add(square_of(seen.position))

    # -- the frontier --------------------------------------------------------

    def _frontier(self, observation: Observation) -> list[GridSquare]:
        """Unknown squares bordering proven-walkable scope squares, nearest first.

        Deterministic twice over: the candidate set is derived from the map's
        own insertion-ordered walk, and the order is a full sort by
        ``(chebyshev distance from here, x, y, z)`` — so the same map and the
        same position always name the same waypoints in the same order. A
        frontier square already answered (visited, swept or skipped) is not
        offered again; a skip stays a skip.

        Membership is the pin's: for a radius pin the frontier cell must lie
        within the sweep (planar Chebyshev from the centre, exactly as the
        loot mission scopes its own); for a room or building pin the anchor —
        the proven-walkable neighbour — must lie in the proven extent, and
        bordering that proof *is* the membership, because the frontier cell
        itself is unknown and cannot testify to its own room.
        """
        pin = self._pin
        assert pin is not None  # every caller runs after _pin_scope
        radius_pin = pin.scope is AreaScope.RADIUS
        here = square_of(observation.player.position)
        found: set[GridSquare] = set()
        for square in self._map.known_squares():
            if not self._map.cell(*square).walkable_hint:
                continue
            if not radius_pin and square not in self._extent:
                continue
            for dx, dy in _PLANAR_STEPS:
                neighbour = (square[0] + dx, square[1] + dy, square[2])
                if neighbour in found or neighbour in self._resolved:
                    continue
                if self._map.cell(*neighbour).known:
                    continue
                if radius_pin:
                    assert pin.centre is not None
                    if _chebyshev(pin.centre, neighbour) > pin.radius:
                        continue
                found.add(neighbour)
        return sorted(found, key=lambda cell: (_chebyshev(here, cell), cell))

    def _start_next_waypoint(self, observation: Observation) -> bool | MissionRefused:
        """Aim a journey at the nearest frontier square. False when none remains."""
        frontier = self._frontier(observation)
        if not frontier:
            return False
        if self._waypoints_started >= self._limits.max_waypoints:
            # Frontier remains and the waypoint budget is spent: the criterion
            # was not met, and saying "complete" would claim a sweep that did
            # not happen. The budget exhaustion is the honest typed ending.
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"the waypoint budget ({self._limits.max_waypoints}) ran out with "
                "frontier remaining",
            )
        waypoint = frontier[0]
        self._waypoints_started += 1
        self._waypoint = waypoint
        self._journey = Journey(
            self._map,
            NavigationTarget(
                x=waypoint[0],
                y=waypoint[1],
                z=waypoint[2],
                radius=WAYPOINT_ARRIVAL_RADIUS,
            ),
            journey_id=f"{self._key_stem[: MAX_JOURNEY_ID_LEN - 8]}.w{self._waypoints_started}",
            limits=self._journey_limits,
        )
        return True

    def _drive_waypoint(self, observation: Observation) -> NextMissionMove:
        waypoint = self._waypoint
        journey = self._journey
        assert waypoint is not None and journey is not None
        if self._map.cell(*waypoint).known:
            # The observations gathered on the approach swept the waypoint
            # into the map: the visit's purpose happened without the arrival,
            # and walking the rest of the way would spend legs on a square
            # with nothing left to teach.
            self._resolve_waypoint()
            return None
        value = journey.next_step(observation)
        if isinstance(value, Arrived):
            self._resolve_waypoint()
            return None
        if isinstance(value, Refused):
            self._skip_after_journey(waypoint, value.error)
            return None
        step: Step = value
        self._pending_action = step.request.action.value
        return MissionStep(request=step.request)

    def _resolve_waypoint(self) -> None:
        waypoint = self._waypoint
        if waypoint is not None:
            self._resolved.add(waypoint)
            self._visited += 1
        self._waypoint = None
        self._journey = None

    def _skip_after_journey(self, waypoint: GridSquare, error: NavigationError) -> None:
        door = _door_named(error.reason_code, error.door_ref)
        if door is not None:
            self._record_skip(waypoint, door)
            return
        if error.reason_code is ReasonCode.PATH_NOT_FOUND:
            # A proven wall is a fact about the world like a locked door; it
            # is the "unreachable, with reason" half of the completion
            # criterion and costs no failure.
            self._record_skip(waypoint, _SKIP_NO_ROUTE)
            return
        self._consecutive_failures += 1
        self._record_skip(waypoint, f"approach refused: {error.failure.value}")

    def _record_skip(self, waypoint: GridSquare, reason: str) -> None:
        self._resolved.add(waypoint)
        self._skipped.append((waypoint, reason))
        self._waypoint = None
        self._journey = None

    # -- endings -------------------------------------------------------------

    def _finish(self, observation: Observation) -> NextMissionMove:
        if self._any_success:
            self._ended = ENDED_COMPLETE
            self._final = MissionComplete()
            return self._final
        if self._probes >= self._limits.max_completion_probes:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                "the completion probe was never observed succeeding",
            )
        # Complete without a single action run — the scope was already known,
        # which is the no-op the radius default anticipates. Nothing exists to
        # succeed the goal with, and minting a result would fabricate
        # evidence; the probe is the engine observing an ordinary fact
        # instead, exactly the loot mission's arrangement. Numbered so a
        # failed probe's retry never reuses a key.
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"explore:{self._key_stem}:done{self._probes}",
                args={
                    "target": {"x": here[0], "y": here[1], "z": here[2]},
                    "radius": 1.0,
                    "max_distance": 1,
                    "allow_doors": True,
                    "allow_stairs": True,
                },
                policy=MOVE_RETRY_POLICY,
            )
        )

    def _refuse(self, ended: str, reason_code: ReasonCode, cause: str) -> MissionRefused:
        self._ended = ended
        refused = MissionRefused(reason_code=reason_code, detail=f"{cause}; {self.summary_line()}")
        self._final = refused
        return refused
