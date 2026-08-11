"""``avoid_threat``: the deterministic retreat mission — the «отступи» half.

The threat directive splits into two halves: «если одиночного зомби безопасно
убить — убей, иначе отступи». Combat is the P4 epic's half; this mission is
the retreat. Where :class:`~pz_agent_core.safety.reflex.ReflexGuard` only
*stops* — the honest emergency answer, standing still next to the danger —
this mission routes *away*: it reads the observed threat picture, picks a
retreat target deterministically, and drives a threat-avoiding
:class:`~pz_agent_core.navigation.Journey` at it. No model call anywhere on
the path: the model (or a shouted «отступай») said "retreat"; every square
after that word is chosen here.

The mission is a state machine over observations, shaped like
:class:`~pz_agent_cli.explore_mission.ExploreMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` drives it through the same seam, with
the same frozen move vocabulary imported from
:mod:`pz_agent_cli.loot_mission`: every retreat leg travels the loop's action
channel as a :class:`~pz_agent_cli.loot_mission.MissionStep` (arriving at one
waypoint is not "safe", so no leg's success may end the goal),
:class:`~pz_agent_cli.loot_mission.MissionProbe` is the goal-seam request of
a mission that never needed to act, and
:class:`~pz_agent_cli.loot_mission.MissionComplete` /
:class:`~pz_agent_cli.loot_mission.MissionRefused` end the goal through the
queue.

Honesty rules this module keeps:

* **The threat picture is the current observation's, every step.** Each
  ``next_step`` re-reads the zombies off the observation it was handed —
  never a snapshot pinned at activation, because zombies move and a retreat
  from where they *were* is a walk into where they are. This is the
  event-driven half of the directive: the picture is re-read when the loop
  hands the mission a fresh observation, not replanned on a timer.
* **Success is observed, never inferred from having moved.** The mission is
  safe when the newest observation reports the nearest zombie at
  :data:`SAFE_DISTANCE` or beyond (or none at all), or the character standing
  in a remembered user safe zone with no chasing zombie observed. Arriving at
  the chosen square proves nothing by itself — the zombies may have followed
  — so arrival without the observed postcondition simply starts the next
  retreat leg.
* **Target selection is deterministic and stated.** The nearest remembered
  user safe zone within :data:`MAX_RETREAT_DISTANCE` wins (ties broken on the
  centre square); with none, the square on the :data:`RETREAT_RING_RADIUS`
  ring that maximises the minimum distance to the observed threats — which is
  the away-vector heuristic in exact form: over a fixed ring, maximising the
  distance to the threat set *is* stepping opposite its centroid, computed
  without a vector that would need tie-breaking of its own.
* **Bounded everything, refusals typed.** Retreat legs, consecutive
  failures and completion probes are each capped, and a retreat that cannot
  open the distance — cornered, unroutable, or out of legs while zombies are
  still observed — ends with :data:`~pz_agent_core.protocol.ReasonCode.
  THREAT_INTERRUPTED` naming the nearest observed threat distance.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Final

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.navigation import (
    MAX_JOURNEY_ID_LEN,
    Arrived,
    Journey,
    JourneyLimits,
    LocalMap,
    NavigationTarget,
    Refused,
    Step,
)
from pz_agent_core.navigation.local_map import GridSquare, square_of
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    JsonDict,
    NearbyZombie,
    Observation,
    ReasonCode,
)

from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    PHASE_APPROACH,
    PHASE_START,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "AVOID_PHASES",
    "ENDED_THREATENED",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_RETREAT_DISTANCE",
    "MAX_RETREAT_LEGS",
    "RETREAT_ARRIVAL_RADIUS",
    "RETREAT_RING_RADIUS",
    "SAFE_DISTANCE",
    "TARGET_OPEN_GROUND",
    "TARGET_SAFE_ZONE",
    "AvoidMission",
    "AvoidMissionLimits",
    "RememberedSafeZone",
    "SafeZones",
]

#: The closed phase set :attr:`AvoidMission.phase` reports — the explore
#: mission's own pair, imported rather than restated: a retreat is either
#: walking a leg (``approach``) or standing between legs (``start``).
AVOID_PHASES: Final = (PHASE_START, PHASE_APPROACH)


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: The observed nearest-zombie distance at or beyond which the retreat has
#: worked, in the observation's own tiles. Deliberately *twice* the threat
#: ladder's reaction range (``ThreatConfig.close_distance`` = 6, pinned by a
#: test rather than imported — this mission does not decide danger levels):
#: a retreat that stopped exactly at the edge of the band the ladder calls
#: HIGH would be declared safe and interrupted again one shamble later.
SAFE_DISTANCE: Final = 12.0

#: How far away (planar Chebyshev) a remembered safe zone's centre may be and
#: still be the retreat target. The single-move distance the mod accepts,
#: exactly the loot radius ceiling's reasoning: a safe zone further than one
#: bounded move is a journey to plan calmly, not a square to flee to.
MAX_RETREAT_DISTANCE: Final = 30

#: The Chebyshev ring around the character on which the open-ground fallback
#: picks its square. Fixed radius rather than "as far as possible" so the
#: candidate set is small (8r squares), deterministic, and lands the target
#: comfortably past :data:`SAFE_DISTANCE` from anything observed nearby.
RETREAT_RING_RADIUS: Final = 15

#: Retreat legs (journeys started) one mission may spend. Each leg is a
#: bounded journey; a retreat that has aimed eight of them and still reads
#: zombies inside the safe distance is not going to outwalk them, and the
#: honest answer is the typed failure, not a ninth leg.
MAX_RETREAT_LEGS: Final = 8

#: Consecutive failed legs (stuck or refused journeys, failed moves) before
#: the mission ends. The other missions' number, restated because missions
#: are allowed to diverge and a shared constant would hide the review.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no action may spend — the loot
#: mission's arrangement: each probe is one goal-seam request, so the goal's
#: own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: How close a retreat leg must bring the character to its chosen square.
#: Wider than a navigation goal's arrival: the square is not the point, the
#: distance from the zombies is, and that is measured off the observation.
RETREAT_ARRIVAL_RADIUS: Final = 2.0

#: Written by the mission when the retreat could not open the distance —
#: cornered, unroutable, or out of legs with zombies still observed. Its own
#: token rather than a reuse of ``no_progress``, because "still threatened"
#: is a fact about the world, not about this mission's budgets.
ENDED_THREATENED: Final = "threatened"

#: The two target kinds a report names. Closed tokens, never free text.
TARGET_SAFE_ZONE: Final = "safe_zone"
TARGET_OPEN_GROUND: Final = "open_ground"


def _chebyshev(a: GridSquare, b: GridSquare) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# --------------------------------------------------------------------------
# ports and limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RememberedSafeZone:
    """One user safe zone, as the memory port hands it to the mission.

    Plain squares and a radius rather than the memory package's own
    :class:`~pz_agent_core.memory.model.SafeZone`, so the mission stays a
    pure function of injected data. :meth:`contains` restates that class's
    own rule — same floor, planar Chebyshev within the radius — and a test
    pins the two against each other so they cannot drift.
    """

    centre: GridSquare
    radius: int

    def __post_init__(self) -> None:
        if self.radius < 0:
            raise ValueError(f"a safe zone radius must be non-negative, got {self.radius}")

    def contains(self, square: GridSquare) -> bool:
        return square[2] == self.centre[2] and _chebyshev(self.centre, square) <= self.radius


#: The one memory question this mission asks, answered fresh per call because
#: the sidecar memory re-loads when the save changes. With nothing wired the
#: honest answer is the empty tuple, and the retreat falls back to open
#: ground — a user who remembered no safe zone gets distance, not a guess at
#: where they would have put one.
SafeZones = Callable[[], tuple[RememberedSafeZone, ...]]


@dataclass(frozen=True, slots=True)
class AvoidMissionLimits:
    """Every bound one mission runs under. All of them are load-bearing."""

    max_retreat_legs: int = MAX_RETREAT_LEGS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        for name in ("max_retreat_legs", "max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# the mission
# --------------------------------------------------------------------------


class AvoidMission:
    """One ``avoid_threat`` goal, driven retreat leg by retreat leg.

    Created by the wrapper when the goal activates and discarded when the
    goal reaches a terminal state — the mission never outlives its goal. All
    inputs are injected and deterministic: the shared
    :class:`~pz_agent_core.navigation.LocalMap`, the safe-zone port and the
    frozen limits. Every journey runs with ``avoid_threats`` forced on —
    a retreat that routed *through* the zombies it is fleeing would be the
    one incoherent journey this stack could build, so the mission does not
    leave the knob to the wrapper's configuration.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        local_map: LocalMap,
        safe_zones: SafeZones,
        limits: AvoidMissionLimits | None = None,
        journey_limits: JourneyLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._key_stem = goal_id[:MAX_JOURNEY_ID_LEN]
        self._map = local_map
        self._safe_zones = safe_zones
        self._limits = limits if limits is not None else AvoidMissionLimits()
        base = journey_limits if journey_limits is not None else JourneyLimits()
        self._journey_limits = replace(base, avoid_threats=True)

        self._journey: Journey | None = None
        self._target_kind: str | None = None
        self._pending_action: str | None = None
        self._legs_started = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False

        self._threats_at_start: int | None = None
        self._nearest_before: float | None = None
        self._nearest_after: float | None = None
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
        """Where the mission stands: one of :data:`AVOID_PHASES`.

        ``approach`` while a journey is walking a retreat leg, ``start``
        between legs (which includes "not yet begun"). Closed tokens from the
        loot mission's vocabulary, never caller or game text; a mission that
        already ended keeps the phase it stopped in, because whether it ended
        is :attr:`ended`'s answer, not this one's.
        """
        return PHASE_APPROACH if self._journey is not None else PHASE_START

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
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        Counts, distances and closed tokens only — a retreat report has no
        game-authored string in it at all. ``nearest_before`` and
        ``nearest_after`` are the observation's own tile distances, ``null``
        while nothing has been observed (or nothing was near).
        """
        return {
            "threats_at_start": self._threats_at_start if self._threats_at_start is not None else 0,
            "nearest_before": self._nearest_before,
            "nearest_after": self._nearest_after,
            "target_kind": self._target_kind,
            "legs_started": self._legs_started,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line of the report, for a goal record's ``detail``.

        Constants and numbers only — no refs, no game-authored names — so it
        satisfies the goal channel's rule that a detail is never caller text.
        """
        return (
            f"avoid {self._ended}: threats_at_start="
            f"{self._threats_at_start if self._threats_at_start is not None else 0} "
            f"nearest_before={_tiles(self._nearest_before)} "
            f"nearest_after={_tiles(self._nearest_after)} legs={self._legs_started}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Every request this mission emits is a journey step, so every result
        is the journey's to learn from; the mission itself only counts the
        success that resets the failure streak and the failure that feeds it.
        Non-terminal acks and results for actions this mission did not ask
        for are ignored — the mission has at most one step in flight.
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
        else:
            self._consecutive_failures += 1

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: learn the map, re-read the threat picture, check the
        observed postcondition (safety beats every budget), then drive the
        current leg or aim a new one. A finished mission replays its terminal
        value. The loop is bounded by the retreat-leg budget: every iteration
        either emits a request, retires a leg, or ends the mission.
        """
        if self._final is not None:
            return self._final
        self._map.observe(observation)
        zombies = _observed_zombies(observation)
        nearest = min((max(0.0, zombie.distance) for zombie in zombies), default=None)
        if self._threats_at_start is None:
            self._threats_at_start = len(zombies)
            self._nearest_before = nearest
        self._nearest_after = nearest
        here = square_of(observation.player.position)

        if self._is_safe(here, zombies, nearest):
            return self._finish(observation)
        if self._pending_action is not None:
            # A channel leg is still being driven; the map learned, the
            # mission decides on the observation after its result lands.
            return None
        while True:
            if self._consecutive_failures >= self._limits.max_consecutive_failures:
                return self._refuse_threatened(
                    f"{self._limits.max_consecutive_failures} consecutive retreat legs failed",
                    nearest,
                )
            if self._journey is None:
                started = self._start_retreat_leg(here, zombies, nearest)
                if isinstance(started, MissionRefused):
                    return started
            journey = self._journey
            assert journey is not None  # _start_retreat_leg either set it or refused
            value = journey.next_step(observation)
            if isinstance(value, Arrived):
                # Arrived and still not safe — the postcondition check above
                # already returned for a safe picture — so the zombies moved
                # with us. Retire the leg and *wait for the next observation*
                # rather than re-aiming from this one: the picture that made
                # this square the answer is the picture that just proved
                # insufficient, and only a fresh one can say something new.
                self._journey = None
                return None
            if isinstance(value, Refused):
                # A refused journey — no route, stuck, budgets — is one
                # failed retreat leg whatever refused it: a wall between
                # here and away counts against the same streak a failed walk
                # does, because both mean the distance is not opening. The
                # loop re-selects immediately; the failure cap above is what
                # keeps a cornered picture from spinning.
                self._consecutive_failures += 1
                self._journey = None
                continue
            step: Step = value
            self._pending_action = step.request.action.value
            return MissionStep(request=step.request)

    # -- the postcondition ---------------------------------------------------

    def _is_safe(
        self, here: GridSquare, zombies: tuple[NearbyZombie, ...], nearest: float | None
    ) -> bool:
        """The observed postcondition, from this observation and nothing else.

        No zombie observed at all satisfies the distance criterion vacuously —
        which is also the no-work activation case the completion probe serves.
        The safe-zone half demands *no chasing zombie observed*, not distance:
        a user's safe zone with a zombie shambling past outside is still the
        fallback point they chose, but one with something actively coming is
        not safe by anyone's definition.
        """
        if nearest is None or nearest >= SAFE_DISTANCE:
            return True
        if any(zombie.chasing for zombie in zombies):
            return False
        return any(zone.contains(here) for zone in self._safe_zones())

    # -- target selection ----------------------------------------------------

    def _start_retreat_leg(
        self, here: GridSquare, zombies: tuple[NearbyZombie, ...], nearest: float | None
    ) -> bool | MissionRefused:
        if self._legs_started >= self._limits.max_retreat_legs:
            return self._refuse_threatened(
                f"the retreat-leg budget ({self._limits.max_retreat_legs}) ran out",
                nearest,
            )
        target = self._select_target(here, zombies)
        if target is None:
            # Zombies observed, none of them locatable, no safe zone in
            # range: there is no direction "away" that is not a guess.
            return self._refuse_threatened("no retreat direction can be derived", nearest)
        square, kind = target
        self._legs_started += 1
        self._target_kind = kind
        self._journey = Journey(
            self._map,
            NavigationTarget(x=square[0], y=square[1], z=square[2], radius=RETREAT_ARRIVAL_RADIUS),
            journey_id=f"{self._key_stem[: MAX_JOURNEY_ID_LEN - 8]}.r{self._legs_started}",
            limits=self._journey_limits,
        )
        return True

    def _select_target(
        self, here: GridSquare, zombies: tuple[NearbyZombie, ...]
    ) -> tuple[GridSquare, str] | None:
        """Where to retreat to, deterministically, from the current picture.

        The nearest remembered user safe zone within
        :data:`MAX_RETREAT_DISTANCE` wins — the user chose it as the place to
        fall back to, and nearest is the one that costs the least exposure to
        reach; ties break on the centre square itself. With no zone in range,
        the open-ground fallback: over the fixed
        :data:`RETREAT_RING_RADIUS` ring, the square maximising the minimum
        distance to the observed threat squares, ties again on the square —
        the away-vector heuristic in exact, tie-broken form. Threat squares
        come only from position-bearing zombies; with none locatable and no
        zone in range there is no honest "away" and the answer is None.
        """
        best_zone: tuple[tuple[int, GridSquare], GridSquare] | None = None
        for zone in self._safe_zones():
            distance = _chebyshev(here, zone.centre)
            if distance > MAX_RETREAT_DISTANCE:
                continue
            key = (distance, zone.centre)
            if best_zone is None or key < best_zone[0]:
                best_zone = (key, zone.centre)
        if best_zone is not None:
            return (best_zone[1], TARGET_SAFE_ZONE)

        threat_squares = tuple(
            square_of(zombie.position) for zombie in zombies if zombie.position is not None
        )
        if not threat_squares:
            return None
        best_square: GridSquare | None = None
        best_key: tuple[int, GridSquare] | None = None
        for candidate in _ring(here, RETREAT_RING_RADIUS):
            clearance = min(_chebyshev(candidate, threat) for threat in threat_squares)
            key = (-clearance, candidate)
            if best_key is None or key < best_key:
                best_key = key
                best_square = candidate
        assert best_square is not None  # the ring is never empty
        return (best_square, TARGET_OPEN_GROUND)

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
        # Safe without a single action run — no threat was observed at
        # activation, or the picture cleared before the first leg moved.
        # Nothing exists to succeed the goal with, and minting a result
        # would fabricate evidence; the probe is the engine observing an
        # ordinary fact instead, exactly the loot mission's arrangement.
        # Numbered so a failed probe's retry never reuses a key.
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"avoid:{self._key_stem}:done{self._probes}",
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

    def _refuse_threatened(self, cause: str, nearest: float | None) -> MissionRefused:
        """The retreat's own typed failure, naming the nearest threat distance.

        ``THREAT_INTERRUPTED`` because that is the fact: the goal ends with
        the threat still inside the safe distance, whatever budget or wall
        stopped the walking.
        """
        self._ended = ENDED_THREATENED
        refused = MissionRefused(
            reason_code=ReasonCode.THREAT_INTERRUPTED,
            detail=(
                f"{cause}; nearest observed threat at {_tiles(nearest)} tiles; "
                f"{self.summary_line()}"
            ),
        )
        self._final = refused
        return refused

    def _refuse(self, ended: str, reason_code: ReasonCode, cause: str) -> MissionRefused:
        self._ended = ended
        refused = MissionRefused(reason_code=reason_code, detail=f"{cause}; {self.summary_line()}")
        self._final = refused
        return refused


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _observed_zombies(observation: Observation) -> tuple[NearbyZombie, ...]:
    """This observation's zombies — the whole threat picture, re-read fresh."""
    if observation.nearby is None:
        return ()
    return tuple(observation.nearby.zombies)


def _tiles(distance: float | None) -> str:
    """A distance for a detail line: one decimal, or the honest 'none'."""
    return "none" if distance is None else f"{distance:.1f}"


def _ring(centre: GridSquare, radius: int) -> Iterator[GridSquare]:
    """The Chebyshev ring around *centre*, same floor, in a fixed scan order.

    Fixed order is what makes the fallback's tie-break deterministic: the
    same centre and radius always yield the same candidates in the same
    sequence, so the same threat picture always picks the same square.
    """
    x, y, z = centre
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if max(abs(dx), abs(dy)) == radius:
                yield (x + dx, y + dy, z)
