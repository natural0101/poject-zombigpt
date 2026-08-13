"""The deterministic goal kinds, served at the planner seam.

The typed goal channel carries ten kinds the goal seam never hands a plan
provider. Four of them no provider serves at all:
:attr:`~pz_agent_core.goals.GoalKind.NAVIGATE_TO` is walked by the route
executor in :mod:`pz_agent_core.navigation`, one observed square at a time;
:attr:`~pz_agent_core.goals.GoalKind.RETURN_HOME` is that same walk with its
target read from the save's remembered home point instead of the submission;
:attr:`~pz_agent_core.goals.GoalKind.LOOT_AREA` is driven by the
deterministic mission in :mod:`pz_agent_cli.loot_mission`, one observed
container at a time; and :attr:`~pz_agent_core.goals.GoalKind.EXPLORE_AREA`
by the deterministic mission in :mod:`pz_agent_cli.explore_mission`, one
frontier square at a time. The other two are the channel's founding kinds:
:attr:`~pz_agent_core.goals.GoalKind.SATISFY_HUNGER` and
:attr:`~pz_agent_core.goals.GoalKind.SATISFY_THIRST` are driven by the
deterministic mission in :mod:`pz_agent_cli.consume_mission`, one bite at a
time — a provider may still *propose* eating on the autonomy loop's own
initiative path, but a goal spoken into the channel is served here, with no
model call anywhere on the path. The care wave adds the last three on the
no-provider terms: :attr:`~pz_agent_core.goals.GoalKind.TREAT_WOUNDS`,
:attr:`~pz_agent_core.goals.GoalKind.REST_UNTIL` and
:attr:`~pz_agent_core.goals.GoalKind.SLEEP_UNTIL_RESTED` are driven by the
deterministic missions in :mod:`pz_agent_cli.care_mission` — a bandage per
observed bleeding wound, one ``survival.rest``, one ``survival.sleep`` —
with every proof owed to the adapters underneath and every adapter refusal
carried to the goal typed and unchanged. The threat wave adds the tenth:
:attr:`~pz_agent_core.goals.GoalKind.AVOID_THREAT` is driven by the
deterministic retreat mission in :mod:`pz_agent_cli.avoid_mission`, one
threat-avoiding journey at a time, with the observed threat picture re-read
every step and the remembered user safe zones answered through the same
memory-port walk the home point takes. The combat wave adds the eleventh:
:attr:`~pz_agent_core.goals.GoalKind.ENGAGE_SINGLE_ZOMBIE` is driven by the
deterministic combat mission in :mod:`pz_agent_cli.combat_mission`, one
policy-gated bounded window at a time — user-submitted only, never minted
by the arbiter or the initiative path. The loop serves every goal through
one seam — :meth:`~pz_agent_cli.runtime.GoalPlanner.propose_for_goal` — so
this module puts all eleven deterministic servers *behind that seam*:
:class:`NavigatingPlanner` wraps whatever planner the app assembled (or
nothing at all), owns one :class:`~pz_agent_core.navigation.LocalMap` per
session, one :class:`~pz_agent_core.navigation.Journey` per navigation or
homeward goal and one mission per loot, explore, consume, care, avoid or
combat goal, answers all eleven kinds itself, and hands every other kind to
the wrapped planner untouched. A loop assembled with no LLM planner still
navigates, loots, explores, comes home, eats, drinks, dresses wounds,
rests, sleeps, retreats and fights on order, because the wrapper is a
complete :class:`~pz_agent_cli.runtime.GoalPlanner` on its own.

The needs arbiter (:mod:`pz_agent_cli.arbiter`) lives behind this same seam:
every observation the wrapper is handed passes through it first, and in
``AUTONOMOUS`` mode a need *crossing* its critical threshold — never a level
held — suspends the active goal through the queue's own levers, injects the
satisfy/care/avoid goal at the front, and leaves resumption to ordinary
activation once the preemptor reaches any terminal state. The wrapper's half
of that promise is drive survival: :func:`_drive_lives` keeps a suspended
goal's journey or mission — candidate list, walked legs and all — so the
resumed goal continues mid-mission instead of starting over.

``return_home`` resolves its target through the same memory-port walk the
loot ports use (:meth:`NavigatingPlanner._loot_ports`): whatever memory the
assembled loop actually holds answers ``home_point()``, and the honest
answers with nothing wired are the honest failures — no home point readable
means a typed ``PRECONDITION_FAILED`` whose detail is the remedy, and a home
on a floor the map knows no stairs to is the Journey's own ``NO_ROUTE``.
``explore_area`` mirrors the loot mission join for join: ordinary steps down
the action channel, the completion probe out the goal seam, the report
sealed into a bounded ledger the goal's death cannot erase
(:meth:`NavigatingPlanner.explore_report`).

A loot mission's steps take the same two routes a journey's do, with one
inversion: for a journey the *final leg* travels the goal seam because its
observed success is the arrival; for a mission **no** ordinary step's success
is the goal's postcondition, so every open, inspect, batch and approach leg
travels the action channel, and the mission ends its goal through the queue —
``succeed`` with the last real channel result's evidence, or ``fail`` with the
mission's typed reason and one-line report summary. The exception is a
mission that finished without a single action running: it emits a bounded
completion probe out the goal seam, whose real observed result the loop
settles the goal with — the same arrangement a journey uses for a target the
character already stands on. A step the channel can no longer report on — its
terminal record evicted from the bounded history, or the channel replaced —
ends the goal ``CAPABILITY_UNAVAILABLE`` on the very next tick
(:data:`UNOBSERVED_STEP_DETAIL`), and so does a step the channel refused to
admit at all (:data:`UNADMITTED_STEP_DETAIL`), rather than leaving the
mission holding a step it can never retire: unlike a journey, a mission gates its own
``next_step`` on that one step in flight, so "learn nothing and replan" is a
thing only the journey path can honestly do. Missions die with their goals
exactly as journeys do, and the mission's full report survives the goal in a
bounded ledger this wrapper keeps (:meth:`NavigatingPlanner.loot_report`).

How a journey's steps reach the engine — and why there are two routes:

* **The final leg travels through the goal seam.** The loop settles a goal on
  the engine's own result (:meth:`SidecarLoop._settle_goal_step`): a succeeded
  action with observed evidence ends the goal ``SUCCEEDED``. That rule is only
  honest for a step whose *own* postcondition is the goal's, and exactly one
  step qualifies: the final leg, whose ``movement.move_to`` carries the
  target's coordinates and the target's arrival radius, so its observed
  success *is* the arrival. It is therefore the one request this wrapper ever
  returns from ``propose_for_goal`` for a route in progress.
* **Every other step travels through the loop's action channel.** An
  intermediate leg or a ``door.open`` retry succeeding must *not* end the goal
  — a door that opened is not an arrival — so those are submitted to
  :class:`~pz_agent_cli.runtime.ActionChannel`, which the loop drains through
  the same gates, budget and engine one tick later. The channel remembers the
  terminal result, which is how the executor's ``note_result`` contract is
  honoured: the next ``propose_for_goal`` reads it back and feeds the journey
  before asking for the next step.

Terminal honesty, both directions. Arrival is declared only by the executor,
only from an observation; when it is declared after a channel-driven leg, the
wrapper ends the goal through :meth:`~pz_agent_core.goals.GoalQueue.succeed`
with that leg's real engine result — the queue's one route to ``SUCCEEDED``,
evidence and all. A refused journey ends the goal through
:meth:`~pz_agent_core.goals.GoalQueue.fail` with the executor's typed reason
(``DOOR_LOCKED``, ``DOOR_BARRICADED``, ``PATH_NOT_FOUND``, ``PATH_STUCK``, or
``NO_PROGRESS`` for an exhausted budget) rather than hanging until the wall
clock gives up. Journeys die with their goals: every call prunes journeys
whose goal is no longer active, and the tracked set is capped.

Two seams this wrapper deliberately does not fight:

* A final leg dispatched through the goal seam has no result path back to the
  journey (the loop keeps engine results to itself there). Its failure charges
  a goal step and the journey simply replans from the next observation — the
  goal's step budget and the journey's own leg/replan budgets bound the
  retries, and a door the observation proves sealed still becomes the typed
  refusal through :meth:`Journey._diagnose_no_route`.
* A leg already admitted to the action channel when the goal is cancelled is
  the engine's to finish — the channel has no per-submission withdrawal — so
  at most one bounded move (≤ 30 squares) completes after a cancel. The
  journey itself is dropped on the next call, and nothing new is submitted.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from enum import Enum
from typing import Final, Protocol

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals import GoalKind, GoalQueue, GoalRecord, GoalState
from pz_agent_core.goals.model import AreaScope, LootScope
from pz_agent_core.loot import LootPolicy
from pz_agent_core.memory import SaveMemory
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
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.selection import NO_CAPABILITIES, CapabilityLookup
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    JsonDict,
    Observation,
    ReasonCode,
)
from pz_agent_mcp.ports import GoalProgress

from .arbiter import NeedsArbiter
from .avoid_mission import (
    AvoidMission,
    AvoidMissionLimits,
    RememberedSafeZone,
)
from .care_mission import (
    CareMissionLimits,
    RestMission,
    SleepMission,
    TreatWoundsMission,
)
from .combat_mission import (
    CombatMissionLimits,
    EngageZombieMission,
)
from .consume_mission import (
    HUNGER,
    THIRST,
    ConsumeMission,
    ConsumeMissionLimits,
    KnownContainers,
    NeedSpec,
    RememberedContainer,
)
from .explore_mission import (
    DEFAULT_EXPLORE_RADIUS,
    ExploreMission,
    ExploreMissionLimits,
)
from .loot_mission import (
    DEFAULT_LOOT_RADIUS,
    ContainerUnchanged,
    IsReserved,
    LootMission,
    LootMissionLimits,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
)
from .runtime import ActionChannel, GoalPlanner, LoopError, Planner

__all__ = [
    "HOME_ARRIVAL_RADIUS",
    "MAX_KEPT_AVOID_REPORTS",
    "MAX_KEPT_CARE_REPORTS",
    "MAX_KEPT_COMBAT_REPORTS",
    "MAX_KEPT_CONSUME_REPORTS",
    "MAX_KEPT_EXPLORE_REPORTS",
    "MAX_KEPT_LOOT_REPORTS",
    "MAX_TRACKED_AVOIDS",
    "MAX_TRACKED_CARES",
    "MAX_TRACKED_COMBATS",
    "MAX_TRACKED_CONSUMES",
    "MAX_TRACKED_EXPLORES",
    "MAX_TRACKED_JOURNEYS",
    "MAX_TRACKED_MISSIONS",
    "MAX_TRACKED_RETURNS",
    "NO_HOME_DETAIL",
    "NO_REST_TARGET_DETAIL",
    "UNADMITTED_STEP_DETAIL",
    "UNOBSERVED_STEP_DETAIL",
    "NavigatingPlanner",
    "NavigationHost",
    "unwrap_planner",
]


def unwrap_planner(planner: Planner | None) -> Planner | None:
    """The planner behind the navigating wrapper, or *planner* itself.

    Assembly and serving code that asks "which planner holds the memory?" or
    "which provider answers?" is asking about the wrapped planner, and this is
    the one place that knows how to look through the wrapper — three call
    sites re-implementing the unwrap is how one of them forgets.
    """
    if isinstance(planner, NavigatingPlanner):
        return planner.inner
    return planner


#: Journeys remembered at once. The queue runs one active goal at a time, so
#: one live journey is the working set and the rest is slack for goals that
#: ended between ticks; the cap exists so a pruning bug could only ever waste
#: this much, never grow without bound.
MAX_TRACKED_JOURNEYS: Final = 4

#: Loot missions remembered at once, for exactly the journeys' reason.
MAX_TRACKED_MISSIONS: Final = 4

#: Homeward journeys remembered at once, again for the journeys' reason: one
#: live drive is the working set, the rest is slack for goals that ended
#: between ticks, and the cap is what a pruning bug could ever cost.
MAX_TRACKED_RETURNS: Final = 4

#: Explore missions remembered at once, same shape, same reason.
MAX_TRACKED_EXPLORES: Final = 4

#: Consume missions remembered at once, same shape, same reason.
MAX_TRACKED_CONSUMES: Final = 4

#: Care missions remembered at once — treat, rest and sleep share one
#: registry because the queue runs one active goal at a time whatever its
#: kind; splitting three fours across three tables would triple the slack a
#: pruning bug could cost without buying a second live drive.
MAX_TRACKED_CARES: Final = 4

#: Avoid missions remembered at once, same shape, same reason.
MAX_TRACKED_AVOIDS: Final = 4

#: Combat missions remembered at once, same shape, same reason.
MAX_TRACKED_COMBATS: Final = 4

#: Finished loot reports kept for reading back. A report is the mission's
#: deliverable and outlives the mission — the goal record can only carry a
#: one-line summary and the evidence key names — but it does not outlive the
#: session, and a bounded ring is the difference between "the last few sweeps
#: are consultable" and a leak shaped like a feature.
MAX_KEPT_LOOT_REPORTS: Final = 8

#: Finished explore reports kept for reading back, in their own ring so a
#: burst of loot goals cannot evict the explore report a user is about to ask
#: for (nor the other way round).
MAX_KEPT_EXPLORE_REPORTS: Final = 8

#: Finished consume reports kept for reading back, in their own ring for the
#: explore ring's reason.
MAX_KEPT_CONSUME_REPORTS: Final = 8

#: Finished care reports kept for reading back, one ring for the three care
#: kinds because they share a registry; a treat report's bandaged-part list
#: is the deliverable the directive's confirmation step reads back.
MAX_KEPT_CARE_REPORTS: Final = 8

#: Finished avoid reports kept for reading back, in their own ring for the
#: explore ring's reason; a retreat report's before/after distances are what
#: "did it actually work" reads back.
MAX_KEPT_AVOID_REPORTS: Final = 8

#: Finished combat reports kept for reading back, in their own ring for the
#: same reason; a combat report's windows/shoves/ended token is the honest
#: ledger of a fight somebody ordered, and it must survive the goal.
MAX_KEPT_COMBAT_REPORTS: Final = 8

#: How close counts as "home". Tighter than a waypoint's radius — the user
#: stood on this exact square when they said "remember home" — but not a
#: pinpoint, because the square itself may be occupied by the furniture they
#: stood beside.
HOME_ARRIVAL_RADIUS: Final = 1.5

#: The remedy, verbatim, for a ``return_home`` with no home to return to.
#: Assembled from constants only, so it satisfies the goal channel's rule
#: that a record's detail is never caller text.
NO_HOME_DETAIL: Final = "no home point is set; stand at home and run: pz-agent remember home"

#: The refusal for a ``rest_until`` record holding no target. Unreachable
#: through the channel — the kind's spec requires the parameter — but a
#: record is constructible without it, and a rest with no target has no
#: postcondition to verify, so it must refuse, never pick one.
NO_REST_TARGET_DETAIL: Final = "the rest_until goal carries no target endurance"

#: The refusal for a mission step whose channel record can no longer be read.
#: :meth:`~pz_agent_cli.runtime.ActionChannel.status` answers ``None`` for
#: three truths — the id was never minted here, its terminal record was
#: evicted from the bounded history, or a previous process minted it — and all
#: three are the same fact to a mission: what became of that step was never
#: observed. Assembled from constants only, so it satisfies the goal channel's
#: rule that a record's detail is never caller text.
UNOBSERVED_STEP_DETAIL: Final = (
    "the step's outcome was never observed; the action channel no longer holds its record"
)

#: What a mission ends with when the channel refused to admit its step. A
#: separate sentence from :data:`UNOBSERVED_STEP_DETAIL` because the two are
#: different facts: there, a record existed and was lost; here, none was ever
#: minted. Both leave the mission holding a step no result can retire.
UNADMITTED_STEP_DETAIL: Final = (
    "the action channel refused the step; no record was minted and no result can arrive"
)

#: Wrapper hops :meth:`NavigatingPlanner._loot_ports` will look through for a
#: ``memory`` attribute — the same walk, with the same slack, that the loop's
#: own ``_container_memory`` performs on the other side of the seam.
_MAX_MEMORY_UNWRAPS: Final = 4

#: Container records the consume ports will read out of a memory in one call.
#: The memory store's own hard ceiling (``memory.CEILINGS['max_containers']``),
#: restated so a foreign memory answering the same port cannot make the walk
#: unbounded; the value is pinned against the store's by a test.
_MAX_KNOWN_CONTAINERS: Final = 512

#: Safe-zone records the avoid port will read out of a memory in one call, on
#: exactly the container ceiling's terms: the store's own hard ceiling
#: (``memory.CEILINGS['max_safe_zones']``), restated and pinned by a test.
_MAX_KNOWN_SAFE_ZONES: Final = 64


class NavigationHost(Protocol):
    """The three loop attributes navigation needs, and nothing else.

    Structural on purpose: :class:`~pz_agent_cli.runtime.SidecarLoop` satisfies
    it as it stands, and a test can satisfy it with a real
    :class:`~pz_agent_core.goals.GoalQueue`, a real lock and a real
    :class:`~pz_agent_cli.runtime.ActionChannel` without assembling a loop.
    The lock is the loop's own :attr:`~pz_agent_cli.runtime.SidecarLoop.goal_lock`
    — the queue is not thread-safe and this wrapper is its second tick-thread
    caller, so it takes the same lock the loop takes, never a second one.
    """

    goals: GoalQueue | None
    goal_lock: threading.Lock
    actions: ActionChannel | None


class _Drive:
    """One journey plus the wrapper's bookkeeping around it."""

    __slots__ = ("journey", "last_success", "pending_action_id", "probes")

    def __init__(self, journey: Journey) -> None:
        self.journey = journey
        #: The channel submission whose terminal result the journey is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel leg produced —
        #: the evidence an observed arrival hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None
        #: Arrival probes emitted for a journey that began already at its
        #: target; numbered so a failed probe's retry never reuses a key.
        self.probes = 0


class _LootDrive:
    """One loot mission plus the wrapper's bookkeeping around it."""

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: LootMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


class _ExploreDrive:
    """One explore mission plus the wrapper's bookkeeping around it.

    Field for field the loot drive's shape, so the shared pending-collection
    helper can serve both without a protocol.
    """

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: ExploreMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


class _ConsumeDrive:
    """One consume mission plus the wrapper's bookkeeping around it.

    Field for field the loot drive's shape, so the shared pending-collection
    helper can serve all three mission drives without a protocol.
    """

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: ConsumeMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


#: The three care missions behind one registry. A union rather than a
#: protocol on purpose: the wrapper types against the concrete missions the
#: care module ships, exactly as it does for loot, explore and consume.
_CareMission = RestMission | SleepMission | TreatWoundsMission


class _CareDrive:
    """One care mission plus the wrapper's bookkeeping around it.

    Field for field the loot drive's shape, so the shared pending-collection
    helper can serve all four mission drives without a protocol.
    """

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: _CareMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


class _AvoidDrive:
    """One avoid mission plus the wrapper's bookkeeping around it.

    Field for field the loot drive's shape, so the shared pending-collection
    helper can serve all five mission drives without a protocol.
    """

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: AvoidMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


class _CombatDrive:
    """One combat mission plus the wrapper's bookkeeping around it.

    Field for field the loot drive's shape, so the shared pending-collection
    helper can serve all six mission drives without a protocol.
    """

    __slots__ = ("last_success", "mission", "pending_action_id")

    def __init__(self, mission: EngageZombieMission) -> None:
        self.mission = mission
        #: The channel submission whose terminal result the mission is owed.
        self.pending_action_id: str | None = None
        #: The most recent succeeded engine result a channel step produced —
        #: the evidence a completed mission hands to ``GoalQueue.succeed``.
        self.last_success: ActionResult | None = None


class _Pending(Enum):
    """What became of the one channel step a mission drive had in flight.

    Private to this module: the six kind handlers read it, nothing else does.
    ``LOST`` is the state the wrapper cannot serve through — see
    :meth:`NavigatingPlanner._collect_mission_pending`.
    """

    RUNNING = "running"
    SETTLED = "settled"
    LOST = "lost"


class NavigatingPlanner:
    """The always-on planner wrapper that walks ``navigate_to`` goals itself.

    Satisfies :class:`~pz_agent_cli.runtime.GoalPlanner` whether or not it
    wraps anything: ``propose`` and every non-navigation goal delegate to the
    wrapped planner when one exists and honestly answer "nothing to propose"
    when none does — never a fabricated action, never a swallowed goal.

    :meth:`bind` hands over the loop after construction (the loop takes the
    planner as a constructor argument, so the two cannot reference each other
    at build time). Until it is called, navigation goals are left untouched
    for their own budgets to expire — the wrapper has no queue to end them
    through, and inventing an end would be worse than reporting none.
    """

    def __init__(
        self,
        inner: Planner | None = None,
        *,
        limits: JourneyLimits | None = None,
        loot_limits: LootMissionLimits | None = None,
        explore_limits: ExploreMissionLimits | None = None,
        consume_limits: ConsumeMissionLimits | None = None,
        care_limits: CareMissionLimits | None = None,
        avoid_limits: AvoidMissionLimits | None = None,
        combat_limits: CombatMissionLimits | None = None,
        loot_memory: object | None = None,
    ) -> None:
        self._inner = inner
        self._limits = limits if limits is not None else JourneyLimits()
        self._loot_limits = loot_limits if loot_limits is not None else LootMissionLimits()
        self._explore_limits = (
            explore_limits if explore_limits is not None else ExploreMissionLimits()
        )
        self._consume_limits = (
            consume_limits if consume_limits is not None else ConsumeMissionLimits()
        )
        self._care_limits = care_limits if care_limits is not None else CareMissionLimits()
        self._avoid_limits = avoid_limits if avoid_limits is not None else AvoidMissionLimits()
        self._combat_limits = combat_limits if combat_limits is not None else CombatMissionLimits()
        #: An explicit memory for the loot ports and the home point, for
        #: assemblies and tests that hold one; when None the wrapper walks
        #: the wrapped planner chain for the ``memory`` the shipped assembly
        #: hangs there.
        self._loot_memory = loot_memory
        self._map = LocalMap()
        self._journeys: OrderedDict[str, _Drive] = OrderedDict()
        self._returns: OrderedDict[str, _Drive] = OrderedDict()
        self._missions: OrderedDict[str, _LootDrive] = OrderedDict()
        self._explores: OrderedDict[str, _ExploreDrive] = OrderedDict()
        self._consumes: OrderedDict[str, _ConsumeDrive] = OrderedDict()
        self._cares: OrderedDict[str, _CareDrive] = OrderedDict()
        self._avoids: OrderedDict[str, _AvoidDrive] = OrderedDict()
        self._combats: OrderedDict[str, _CombatDrive] = OrderedDict()
        self._loot_reports: OrderedDict[str, JsonDict] = OrderedDict()
        self._explore_reports: OrderedDict[str, JsonDict] = OrderedDict()
        self._consume_reports: OrderedDict[str, JsonDict] = OrderedDict()
        self._care_reports: OrderedDict[str, JsonDict] = OrderedDict()
        self._avoid_reports: OrderedDict[str, JsonDict] = OrderedDict()
        self._combat_reports: OrderedDict[str, JsonDict] = OrderedDict()
        #: The needs arbiter: interrupt, satisfy, resume (see
        #: :mod:`pz_agent_cli.arbiter`). It reads the critical thresholds
        #: through the same per-call policy walk the consume and care
        #: missions use, so the lines it preempts at are the lines the
        #: injected missions aim for.
        self._arbiter = NeedsArbiter(policy=lambda: self._policy_ports()[0])
        self._host: NavigationHost | None = None

    # -- wiring -------------------------------------------------------------

    def bind(self, host: NavigationHost) -> None:
        """Point the wrapper at the loop whose queue and channel it serves."""
        self._host = host

    @property
    def inner(self) -> Planner | None:
        """The wrapped planner, for assembly code that must see through this."""
        return self._inner

    @property
    def map(self) -> LocalMap:
        """The session's local map. Shared by every journey; read it freely."""
        return self._map

    @property
    def tracked_journeys(self) -> int:
        return len(self._journeys)

    @property
    def tracked_returns(self) -> int:
        return len(self._returns)

    @property
    def tracked_missions(self) -> int:
        return len(self._missions)

    @property
    def tracked_explores(self) -> int:
        return len(self._explores)

    @property
    def tracked_consumes(self) -> int:
        return len(self._consumes)

    @property
    def tracked_cares(self) -> int:
        return len(self._cares)

    @property
    def tracked_avoids(self) -> int:
        return len(self._avoids)

    @property
    def tracked_combats(self) -> int:
        return len(self._combats)

    def loot_report(self, goal_id: str) -> JsonDict | None:
        """The loot report for *goal_id*: live while it runs, kept when it ends.

        A live mission answers its current snapshot; a finished or abandoned
        one answers the sealed report from the bounded ledger. None for a
        goal this wrapper never drove a mission for, or one whose report has
        been evicted by :data:`MAX_KEPT_LOOT_REPORTS` newer ones.
        """
        drive = self._missions.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._loot_reports.get(goal_id)

    def explore_report(self, goal_id: str) -> JsonDict | None:
        """The explore report for *goal_id*, on the loot report's exact terms."""
        drive = self._explores.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._explore_reports.get(goal_id)

    def consume_report(self, goal_id: str) -> JsonDict | None:
        """The consume report for *goal_id*, on the loot report's exact terms."""
        drive = self._consumes.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._consume_reports.get(goal_id)

    def care_report(self, goal_id: str) -> JsonDict | None:
        """The care report for *goal_id*, on the loot report's exact terms.

        One accessor for the three care kinds because they share a registry;
        a treat report carries the verified ``wounds_bandaged`` list, a rest
        or sleep report the one action's request-and-outcome facts.
        """
        drive = self._cares.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._care_reports.get(goal_id)

    def avoid_report(self, goal_id: str) -> JsonDict | None:
        """The avoid report for *goal_id*, on the loot report's exact terms."""
        drive = self._avoids.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._avoid_reports.get(goal_id)

    def combat_report(self, goal_id: str) -> JsonDict | None:
        """The combat report for *goal_id*, on the loot report's exact terms.

        The fight's honest ledger — windows fought, shoves, the refusal
        token, the ended token — live while the mission runs, sealed when
        the goal ends however it ends.
        """
        drive = self._combats.get(goal_id)
        if drive is not None:
            return drive.mission.report
        return self._combat_reports.get(goal_id)

    @property
    def arbiter_log(self) -> tuple[JsonDict, ...]:
        """The needs arbiter's bounded decision ledger, oldest first.

        Exposed on the mission reports' terms: read-only, copies on the way
        out, safe without the goal lock because every entry is replaced whole
        (see :attr:`~pz_agent_cli.arbiter.NeedsArbiter.log`).
        """
        return self._arbiter.log

    def goal_progress(self, goal_id: str) -> GoalProgress | None:
        """The live drive's phase and counters for *goal_id*, or ``None``.

        Covers every deterministic kind through its registry: a
        journey answers its executor state (``planning``/``moving``/
        ``arrived``/``refused``) with the legs walked so far, a mission its
        pipeline phase with its report's own counts. ``None`` is the honest
        answer everywhere else — a delegated kind an LLM planner serves has no
        deterministic phase, an unknown id names no drive, and a drive pruned
        with its finished goal reports nothing rather than a stale phase.

        Read-only over the registries and the drives' own read-only state, so
        the status thread may call it without the goal lock: every value read
        is replaced whole, never mutated in place, and a torn read across two
        ticks yields one tick's honest answer or the other's.
        """
        drive = self._journeys.get(goal_id)
        if drive is None:
            drive = self._returns.get(goal_id)
        if drive is not None:
            journey = drive.journey
            return GoalProgress(
                phase=journey.state.value,
                counters={"legs_used": journey.legs_used},
            )
        loot = self._missions.get(goal_id)
        if loot is not None:
            report = loot.mission.report
            return GoalProgress(
                phase=loot.mission.phase,
                counters={
                    "containers_inspected": len(report["containers_inspected"]),
                    "containers_skipped": len(report["containers_skipped"]),
                },
            )
        explore = self._explores.get(goal_id)
        if explore is not None:
            report = explore.mission.report
            return GoalProgress(
                phase=explore.mission.phase,
                counters={
                    "waypoints_visited": report["waypoints_visited"],
                    "cells_discovered": report["cells_discovered"],
                },
            )
        consume = self._consumes.get(goal_id)
        if consume is not None:
            report = consume.mission.report
            return GoalProgress(
                phase=consume.mission.phase,
                counters={
                    "candidates_tried": report["candidates_tried"],
                    "consumed": len(report["consumed"]),
                    "skipped": len(report["skipped"]),
                },
            )
        care = self._cares.get(goal_id)
        if care is not None:
            mission = care.mission
            report = mission.report
            if isinstance(mission, TreatWoundsMission):
                counters = {
                    "wounds_bandaged": len(report["wounds_bandaged"]),
                    "bleeding_remaining": report["bleeding_remaining"],
                }
            else:
                # Rest and sleep are one-action drives; the honest counter is
                # whether that action has been asked for, as 0 or 1.
                counters = {"requested": 1 if report["requested"] else 0}
            return GoalProgress(phase=mission.phase, counters=counters)
        avoid = self._avoids.get(goal_id)
        if avoid is not None:
            report = avoid.mission.report
            threats = report["threats_at_start"]
            legs = report["legs_started"]
            return GoalProgress(
                phase=avoid.mission.phase,
                counters={
                    # The report's own ints, re-narrowed because a JsonDict
                    # value is typed wide; the distances stay in the report —
                    # counters carry counts, not measurements.
                    "threats_at_start": threats if isinstance(threats, int) else 0,
                    "legs_started": legs if isinstance(legs, int) else 0,
                },
            )
        combat = self._combats.get(goal_id)
        if combat is not None:
            report = combat.mission.report
            windows = report["windows_fought"]
            shoves = report["shoves"]
            return GoalProgress(
                phase=combat.mission.phase,
                counters={
                    # Same re-narrowing as the avoid branch; the refusal and
                    # ended tokens stay in the report — counters carry counts.
                    "windows_fought": windows if isinstance(windows, int) else 0,
                    "shoves": shoves if isinstance(shoves, int) else 0,
                },
            )
        return None

    # -- the Planner half ----------------------------------------------------

    def propose(self, observation: Observation) -> ActionRequest | None:
        """Feed the map, drop dead journeys, and let the wrapped planner speak."""
        self._map.observe(observation)
        self._prune()
        # The idle-side arbiter hook: a need crossing its threshold with no
        # goal active still injects the needs goal — autonomy reacting to its
        # own needs — and the loop's next tick activates and serves it.
        self._arbiter.observe(observation, self._host)
        if self._inner is None:
            return None
        return self._inner.propose(observation)

    # -- the GoalPlanner half ------------------------------------------------

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        """Serve the deterministic kinds ourselves; delegate everything else."""
        # The arbiter sees the observation before the goal is served: a need
        # crossing its threshold suspends the active goal and injects the
        # needs goal *now*, and the kind handler below then reads the parked
        # record and declines the tick instead of stepping suspended work.
        self._arbiter.observe(observation, self._host)
        if goal.kind is PlannerGoalKind.NAVIGATE_TO:
            return self._navigate(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.RETURN_HOME:
            return self._return_home(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.LOOT_AREA:
            return self._loot(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.EXPLORE_AREA:
            return self._explore(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.SATISFY_HUNGER:
            return self._consume(goal.goal_id, observation, need=HUNGER)
        if goal.kind is PlannerGoalKind.SATISFY_THIRST:
            return self._consume(goal.goal_id, observation, need=THIRST)
        if goal.kind in (
            PlannerGoalKind.TREAT_WOUNDS,
            PlannerGoalKind.REST_UNTIL,
            PlannerGoalKind.SLEEP_UNTIL_RESTED,
        ):
            # One registry serves the three care kinds; which mission to
            # build is the record's own kind's answer, read in _care.
            return self._care(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.AVOID_THREAT:
            return self._avoid(goal.goal_id, observation)
        if goal.kind is PlannerGoalKind.ENGAGE_SINGLE_ZOMBIE:
            return self._combat(goal.goal_id, observation)
        self._map.observe(observation)
        self._prune()
        inner = self._inner
        if isinstance(inner, GoalPlanner):
            return inner.propose_for_goal(goal, observation)
        # A goal-shaped ask and no goal-capable planner behind the seam:
        # nothing serves it this tick, and the channel's budgets bound it.
        return None

    # -- navigation ----------------------------------------------------------

    def _navigate(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        host = self._host
        if host is None or host.goals is None:
            # Unbound (or a loop without a goal queue, which cannot activate a
            # goal in the first place): learn from the observation and decline.
            # Ending the goal is the queue's privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._journeys.pop(goal_id, None)
            return None
        target = self._target_of(record)
        if target is None:
            # Unreachable through the channel — GoalRequest requires all three
            # coordinates — but a record is constructible without them, and a
            # journey with no destination must refuse, not guess one.
            with host.goal_lock:
                queue.fail(
                    goal_id,
                    ReasonCode.INVALID_ARGUMENT,
                    "the navigate_to goal carries no complete target square",
                )
            self._map.observe(observation)
            return None

        drive = self._journeys.get(goal_id)
        if drive is None:
            drive = _Drive(
                Journey(
                    self._map,
                    target,
                    journey_id=goal_id[:MAX_JOURNEY_ID_LEN],
                    limits=self._limits,
                )
            )
            self._journeys[goal_id] = drive
            self._enforce_cap()
        return self._serve_journey(
            host, goal_id, drive, observation, registry=self._journeys, probe_prefix="nav"
        )

    def _return_home(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one homeward journey, mirroring :meth:`_navigate` join for join.

        The only structural difference is where the target comes from: the
        save's remembered home point, read through the same memory-port walk
        the loot ports use. No home readable is a typed failure whose detail
        *is* the remedy; a home the map knows no route to — another floor
        with no stairs learned, a proven wall — is the Journey's own
        ``NO_ROUTE``, ended exactly as a navigation goal's would be.
        """
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn
            # from the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._returns.pop(goal_id, None)
            return None

        drive = self._returns.get(goal_id)
        if drive is None:
            home = self._home_point()
            if home is None:
                # Either no memory is wired or the memory holds no home; both
                # honestly mean no home point is readable here, and the one
                # remedy fixes both.
                with host.goal_lock:
                    queue.fail(goal_id, ReasonCode.PRECONDITION_FAILED, NO_HOME_DETAIL)
                self._map.observe(observation)
                return None
            drive = _Drive(
                Journey(
                    self._map,
                    NavigationTarget(x=home[0], y=home[1], z=home[2], radius=HOME_ARRIVAL_RADIUS),
                    journey_id=goal_id[:MAX_JOURNEY_ID_LEN],
                    limits=self._limits,
                )
            )
            self._returns[goal_id] = drive
            self._enforce_cap()
        return self._serve_journey(
            host, goal_id, drive, observation, registry=self._returns, probe_prefix="home"
        )

    def _serve_journey(
        self,
        host: NavigationHost,
        goal_id: str,
        drive: _Drive,
        observation: Observation,
        *,
        registry: OrderedDict[str, _Drive],
        probe_prefix: str,
    ) -> ActionRequest | None:
        """The journey-driving core both journey-shaped kinds share.

        One body on purpose: the final-leg/goal-seam rule, the channel
        backpressure, the arrival evidence and the typed refusals are the
        same contract for a submitted target and a remembered one, and two
        copies of the contract is how one of them drifts.
        """
        channel = host.actions
        waiting = self._collect_pending(drive, channel)
        if waiting:
            # The channel leg is still being driven. The map still learns from
            # this tick's observation; the journey decides on the next one.
            self._map.observe(observation)
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the journey for a step it could not submit would burn
            # a leg for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.journey.next_step(observation)
        if isinstance(value, Arrived):
            return self._finish_arrived(
                host, goal_id, drive, observation, registry=registry, probe_prefix=probe_prefix
            )
        if isinstance(value, Refused):
            self._finish_refused(host, goal_id, value.error, registry=registry)
            return None
        return self._dispatch_step(host, goal_id, drive, value, registry=registry)

    def _collect_pending(self, drive: _Drive, channel: ActionChannel | None) -> bool:
        """Fold a finished channel leg into the journey. True while one is running."""
        action_id = drive.pending_action_id
        if action_id is None or channel is None:
            return False
        record = channel.status(action_id)
        if record is None:
            # Evicted, or a restarted channel: whatever happened to the leg was
            # never observed, so the journey learns nothing and replans.
            drive.pending_action_id = None
            return False
        if not record.terminal:
            return True
        drive.pending_action_id = None
        result = record.result
        if result is not None:
            drive.journey.note_result(result)
            if result.status is ActionStatus.SUCCEEDED:
                drive.last_success = result
        return False

    def _dispatch_step(
        self,
        host: NavigationHost,
        goal_id: str,
        drive: _Drive,
        step: Step,
        *,
        registry: OrderedDict[str, _Drive],
    ) -> ActionRequest | None:
        """Route one executor step: final leg out the seam, the rest via the channel."""
        final_leg = step.door_ref is None and step.leg_target == drive.journey.target.square
        if final_leg:
            # The loop settles the goal on this request's own result, and that
            # is honest here and only here: the move carries the target square
            # and the target radius, so its observed success is the arrival.
            return step.request
        channel = host.actions
        if channel is None:
            # Assigning None after construction is expressible on the loop, and
            # a wrapper with no conveyor for intermediate work cannot navigate
            # honestly — a multi-leg route would end its goal on leg one.
            self._finish_refused(
                host,
                goal_id,
                _WrapperRefusal(
                    "the loop holds no action channel for intermediate legs",
                    ReasonCode.CAPABILITY_UNAVAILABLE,
                ),
                registry=registry,
            )
            return None
        try:
            admitted = channel.submit(step.request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now, or a restart made the key ambiguous. The leg is dropped,
            # the journey replans from the next observation, and its own leg
            # and replan budgets bound how often this can repeat.
            return None
        drive.pending_action_id = admitted.action_id
        return None

    def _finish_arrived(
        self,
        host: NavigationHost,
        goal_id: str,
        drive: _Drive,
        observation: Observation,
        *,
        registry: OrderedDict[str, _Drive],
        probe_prefix: str,
    ) -> ActionRequest | None:
        """End an arrived journey's goal, on evidence something actually observed."""
        queue = host.goals
        assert queue is not None  # _serve_journey's callers returned without one
        last = drive.last_success
        if last is not None:
            # The executor read arrival off the observation; the result that
            # produced it is real engine output with observed evidence, which
            # is the only currency GoalQueue.succeed accepts.
            with host.goal_lock:
                queue.succeed(goal_id, last)
            registry.pop(goal_id, None)
            return None
        # Arrived before anything ran — the goal named a square the character
        # already stands on. No result exists to succeed with, and minting one
        # here would fabricate evidence; instead the engine is asked to observe
        # the fact: a move to the target inside its own radius, whose success
        # the loop settles the goal on. Numbered so a failed probe never
        # replays its predecessor's cached refusal.
        target = drive.journey.target
        drive.probes += 1
        return ActionRequest(
            action=ActionName.MOVEMENT_MOVE_TO,
            session_id=observation.session_id,
            idempotency_key=f"{probe_prefix}:{goal_id[:MAX_JOURNEY_ID_LEN]}:arrive{drive.probes}",
            args={
                "target": {"x": target.x, "y": target.y, "z": target.z},
                "radius": target.radius,
                "max_distance": 1,
                "allow_doors": True,
                "allow_stairs": True,
            },
            policy=MOVE_RETRY_POLICY,
        )

    def _finish_refused(
        self,
        host: NavigationHost,
        goal_id: str,
        error: object,
        *,
        registry: OrderedDict[str, _Drive],
    ) -> None:
        """End the goal with the executor's typed reason. Never hangs it."""
        queue = host.goals
        if queue is None:
            return
        reason, failure = _reason_of(error)
        with host.goal_lock:
            queue.fail(goal_id, reason, f"navigation refused: {failure}")
        registry.pop(goal_id, None)

    def _home_point(self) -> tuple[int, int, int] | None:
        """The remembered home square, through the memory-port walk, or None.

        None covers every way a home cannot be read — no memory wired, a
        memory holding no home, or a foreign memory answering something that
        is not a grid square — because the remedy is the same sentence for
        all of them: stand at home and remember it again. The shipped store
        only ever writes integer squares, so the malformed branch exists for
        the walk's honesty, not for any assembly this project ships.
        """
        memory: object | None = self._loot_memory
        if memory is None or not callable(getattr(memory, "home_point", None)):
            memory = None
            candidate: object | None = self._inner
            for _ in range(_MAX_MEMORY_UNWRAPS):
                if candidate is None:
                    break
                found = getattr(candidate, "memory", None)
                if found is not None and callable(getattr(found, "home_point", None)):
                    memory = found
                    break
                candidate = getattr(candidate, "inner", None)
        if memory is None:
            return None
        answer = getattr(memory, "home_point", None)
        home = answer() if callable(answer) else None
        if home is None:
            return None
        x = getattr(home, "x", None)
        y = getattr(home, "y", None)
        z = getattr(home, "z", None)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (x, y, z)):
            return None
        assert isinstance(x, int) and isinstance(y, int) and isinstance(z, int)
        return (x, y, z)

    # -- loot ----------------------------------------------------------------

    def _loot(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one loot mission, mirroring :meth:`_navigate` join for join."""
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            # A suspension marker keeps the drive: the goal is coming back,
            # and the candidate list it was interrupted over comes back with
            # it. Anything else parked here is a dead goal's leftovers.
            if not _drive_lives(record):
                self._missions.pop(goal_id, None)
            return None

        drive = self._missions.get(goal_id)
        if drive is None:
            drive = _LootDrive(self._mission_for(goal_id, record))
            self._missions[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The mission still holds that step in flight and can never
            # retire it, so it would decline every tick from here and no goal
            # step would ever be charged: end the goal typed instead (see
            # _collect_mission_pending).
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_mission_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_mission_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_mission_complete(host, goal_id, drive)
            return None
        self._finish_mission_refused(host, goal_id, drive, value)
        return None

    def _mission_for(self, goal_id: str, record: GoalRecord) -> LootMission:
        """Build the mission the goal's own validated parameters describe."""
        params = record.params
        categories = params.loot_categories()
        policy = LootPolicy(
            wanted=categories if categories is not None else frozenset(),
            take_all=bool(params.take_all),
        )
        is_reserved, container_unchanged = self._loot_ports()
        return LootMission(
            goal_id,
            local_map=self._map,
            scope=params.scope if params.scope is not None else LootScope.ROOM,
            radius=params.radius if params.radius is not None else DEFAULT_LOOT_RADIUS,
            policy=policy,
            is_reserved=is_reserved,
            container_unchanged=container_unchanged,
            limits=self._loot_limits,
            journey_limits=self._limits,
        )

    def _loot_ports(self) -> tuple[IsReserved, ContainerUnchanged]:
        """The mission's two memory questions, answered by whatever is wired.

        The reserve question is served by any memory carrying
        ``reserves_item`` — :class:`~pz_agent_cli.memory.SidecarMemory`, the
        autonomy fallbacks, or a bare
        :class:`~pz_agent_core.memory.SaveMemory` — and the unchanged
        question by a ``container_unchanged`` on the memory itself or on the
        :class:`~pz_agent_core.memory.SaveMemory` behind its ``loaded``
        attribute, resolved *per call* because the sidecar memory re-loads
        when the save changes. With nothing wired the honest constants apply:
        nothing is reserved, and nothing is provably unchanged, so the
        mission goes and looks.
        """
        memory = self._loot_memory
        if memory is None:
            candidate: object | None = self._inner
            for _ in range(_MAX_MEMORY_UNWRAPS):
                if candidate is None:
                    break
                found = getattr(candidate, "memory", None)
                if found is not None and callable(getattr(found, "reserves_item", None)):
                    memory = found
                    break
                candidate = getattr(candidate, "inner", None)
        if memory is None:
            return (lambda full_type: False), (lambda tail, revision: False)

        def is_reserved(full_type: str, *, source: object = memory) -> bool:
            answer = getattr(source, "reserves_item", None)
            return bool(answer(full_type)) if callable(answer) else False

        def container_unchanged(tail: str, revision: str, *, source: object = memory) -> bool:
            direct = getattr(source, "container_unchanged", None)
            if callable(direct):
                return bool(direct(tail, revision))
            loaded = getattr(source, "loaded", None)
            if isinstance(loaded, SaveMemory):
                return loaded.container_unchanged(tail, revision)
            return False

        return is_reserved, container_unchanged

    def _collect_mission_pending(
        self,
        drive: _LootDrive | _ExploreDrive | _ConsumeDrive | _CareDrive | _AvoidDrive | _CombatDrive,
        channel: ActionChannel | None,
    ) -> _Pending:
        """Fold a finished channel step into the mission, and say what happened.

        Three answers because there are three facts, and the middle one used
        to be told as the last: ``RUNNING`` while the step is still being
        driven, ``SETTLED`` once its terminal result has been folded in (or
        there was nothing in flight to fold), and ``LOST`` when the channel
        can no longer say — an evicted record, an id from a previous process.
        A lost step is not a replan: the mission still holds it as its one
        step in flight, so it can neither learn the outcome nor step past it,
        and only the caller can end the goal. See :data:`UNOBSERVED_STEP_DETAIL`.
        """
        action_id = drive.pending_action_id
        if action_id is None or channel is None:
            return _Pending.SETTLED
        record = channel.status(action_id)
        if record is None:
            drive.pending_action_id = None
            return _Pending.LOST
        if not record.terminal:
            return _Pending.RUNNING
        drive.pending_action_id = None
        result = record.result
        if result is not None:
            drive.mission.note_result(result)
            if result.status is ActionStatus.SUCCEEDED:
                drive.last_success = result
        return _Pending.SETTLED

    def _submit_mission_step(
        self, host: NavigationHost, goal_id: str, drive: _LootDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot loot honestly.
            drive.mission.mark_abandoned()
            self._finish_mission_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_mission_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_mission_complete(
        self, host: NavigationHost, goal_id: str, drive: _LootDrive
    ) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _loot returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_report(goal_id, drive)

    def _finish_mission_refused(
        self, host: NavigationHost, goal_id: str, drive: _LootDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_report(goal_id, drive)

    def _seal_report(self, goal_id: str, drive: _LootDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._loot_reports[goal_id] = drive.mission.report
        self._loot_reports.move_to_end(goal_id)
        while len(self._loot_reports) > MAX_KEPT_LOOT_REPORTS:
            self._loot_reports.popitem(last=False)
        self._missions.pop(goal_id, None)

    # -- explore -------------------------------------------------------------

    def _explore(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one explore mission, mirroring :meth:`_loot` join for join."""
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._explores.pop(goal_id, None)
            return None

        drive = self._explores.get(goal_id)
        if drive is None:
            drive = _ExploreDrive(self._explore_mission_for(goal_id, record))
            self._explores[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The loot path's reasoning, verbatim: a step whose outcome was
            # never observed wedges the mission, so the goal ends typed.
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_explore_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_explore_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_explore_complete(host, goal_id, drive)
            return None
        self._finish_explore_refused(host, goal_id, drive, value)
        return None

    def _explore_mission_for(self, goal_id: str, record: GoalRecord) -> ExploreMission:
        """Build the mission the goal's own validated parameters describe.

        The absent scope reads as RADIUS — explore's own default, pinned in
        the kind's spec: the room the character stands in is the one patch of
        world already observed, so exploring it is a no-op, while the bounded
        sweep is what the bare goal plausibly asks for.
        """
        params = record.params
        return ExploreMission(
            goal_id,
            local_map=self._map,
            scope=params.scope if params.scope is not None else AreaScope.RADIUS,
            radius=params.radius if params.radius is not None else DEFAULT_EXPLORE_RADIUS,
            limits=self._explore_limits,
            journey_limits=self._limits,
        )

    def _submit_explore_step(
        self, host: NavigationHost, goal_id: str, drive: _ExploreDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot explore honestly.
            drive.mission.mark_abandoned()
            self._finish_explore_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_explore_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_explore_complete(
        self, host: NavigationHost, goal_id: str, drive: _ExploreDrive
    ) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _explore returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_explore_report(goal_id, drive)

    def _finish_explore_refused(
        self, host: NavigationHost, goal_id: str, drive: _ExploreDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_explore_report(goal_id, drive)

    def _seal_explore_report(self, goal_id: str, drive: _ExploreDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._explore_reports[goal_id] = drive.mission.report
        self._explore_reports.move_to_end(goal_id)
        while len(self._explore_reports) > MAX_KEPT_EXPLORE_REPORTS:
            self._explore_reports.popitem(last=False)
        self._explores.pop(goal_id, None)

    # -- consume -------------------------------------------------------------

    def _consume(
        self, goal_id: str, observation: Observation, *, need: NeedSpec
    ) -> ActionRequest | None:
        """Drive one consume mission, mirroring :meth:`_loot` join for join."""
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._consumes.pop(goal_id, None)
            return None

        drive = self._consumes.get(goal_id)
        if drive is None:
            drive = _ConsumeDrive(self._consume_mission_for(goal_id, record, need))
            self._consumes[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The loot path's reasoning, verbatim: a step whose outcome was
            # never observed wedges the mission, so the goal ends typed.
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_consume_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_consume_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_consume_complete(host, goal_id, drive)
            return None
        self._finish_consume_refused(host, goal_id, drive, value)
        return None

    def _consume_mission_for(
        self, goal_id: str, record: GoalRecord, need: NeedSpec
    ) -> ConsumeMission:
        """Build the mission the goal's own validated parameters describe.

        ``satisfy_to`` is the one parameter these kinds carry; absent, the
        mission reads the policy's own satisfied threshold — the same number
        the plan providers aimed for when they served these kinds, so the
        reroute moves the server, never the target.
        """
        is_reserved, known_containers, policy, capabilities = self._consume_ports()
        return ConsumeMission(
            goal_id,
            need=need,
            local_map=self._map,
            policy=policy,
            is_reserved=is_reserved,
            known_containers=known_containers,
            satisfy_to=record.params.satisfy_to,
            capabilities=capabilities,
            limits=self._consume_limits,
            journey_limits=self._limits,
        )

    def _consume_ports(
        self,
    ) -> tuple[IsReserved, KnownContainers, PolicyConfig, CapabilityLookup]:
        """The consume mission's four ports, answered by whatever is wired.

        The reserve question is :meth:`_loot_ports`'s own, verbatim — one
        precedence rule, one resolution. Container knowledge is served by a
        ``containers`` callable on the memory itself or on the
        :class:`~pz_agent_core.memory.SaveMemory` behind its ``loaded``
        attribute, resolved *per call* because the sidecar memory re-loads
        when the save changes; with nothing wired the honest answer is the
        empty tuple, and the mission fetches only from what it can see. The
        policy and the capability lookup are read off the wrapped planner
        chain (the shipped :class:`~pz_agent_cli.autonomy.AutonomyPlanner`
        hangs both there); the honest fallbacks are the default policy and
        :data:`~pz_agent_core.policy.selection.NO_CAPABILITIES` — unproven
        capabilities are unusable, so a bite defaults to a whole unit.
        """
        is_reserved, _ = self._loot_ports()

        def known_containers() -> tuple[RememberedContainer, ...]:
            memory = self._memory_with("reserves_item")
            if memory is None:
                return ()
            records: object = None
            direct = getattr(memory, "containers", None)
            if callable(direct):
                records = direct()
            else:
                loaded = getattr(memory, "loaded", None)
                if isinstance(loaded, SaveMemory):
                    records = loaded.containers()
            if not isinstance(records, tuple):
                return ()
            out: list[RememberedContainer] = []
            # Bounded by the memory store's own container ceiling; the slice
            # restates it so a foreign memory cannot make this walk unbounded.
            for record in records[:_MAX_KNOWN_CONTAINERS]:
                tail = getattr(record, "tail", None)
                if not isinstance(tail, str) or not tail:
                    continue
                raw_square = getattr(record, "square", None)
                square: tuple[int, int, int] | None = None
                if raw_square is not None:
                    x = getattr(raw_square, "x", None)
                    y = getattr(raw_square, "y", None)
                    z = getattr(raw_square, "z", None)
                    if (
                        isinstance(x, int)
                        and isinstance(y, int)
                        and isinstance(z, int)
                        and not any(isinstance(value, bool) for value in (x, y, z))
                    ):
                        square = (x, y, z)
                raw_categories = getattr(record, "categories", ())
                categories = frozenset(
                    category for category in raw_categories if isinstance(category, str)
                )
                inspected = getattr(record, "item_count", -1)
                out.append(
                    RememberedContainer(
                        tail=tail,
                        square=square,
                        categories=categories,
                        inspected=isinstance(inspected, int) and inspected >= 0,
                    )
                )
            return tuple(out)

        policy, capabilities = self._policy_ports()
        return (is_reserved, known_containers, policy, capabilities)

    def _policy_ports(self) -> tuple[PolicyConfig, CapabilityLookup]:
        """The policy and capability lookup off the wrapped planner chain.

        The shipped :class:`~pz_agent_cli.autonomy.AutonomyPlanner` hangs
        both there; the honest fallbacks are the default policy and
        :data:`~pz_agent_core.policy.selection.NO_CAPABILITIES`. Factored
        because the consume and care missions read the same chain, and two
        copies of the walk is how one of them forgets a hop.
        """
        policy: PolicyConfig | None = None
        capabilities: CapabilityLookup | None = None
        candidate: object | None = self._inner
        for _ in range(_MAX_MEMORY_UNWRAPS):
            if candidate is None or (policy is not None and capabilities is not None):
                break
            found_policy = getattr(candidate, "policy", None)
            if policy is None and isinstance(found_policy, PolicyConfig):
                policy = found_policy
            found_capabilities = getattr(candidate, "capabilities", None)
            if capabilities is None and isinstance(found_capabilities, CapabilityLookup):
                capabilities = found_capabilities
            candidate = getattr(candidate, "inner", None)
        return (
            policy if policy is not None else DEFAULT_POLICY_CONFIG,
            capabilities if capabilities is not None else NO_CAPABILITIES,
        )

    def _memory_with(self, attribute: str) -> object | None:
        """The wired memory carrying a callable *attribute*, or None.

        The same walk :meth:`_loot_ports` and :meth:`_home_point` perform,
        factored so the consume ports do not become a fourth copy of it.
        """
        memory: object | None = self._loot_memory
        if memory is not None and callable(getattr(memory, attribute, None)):
            return memory
        candidate: object | None = self._inner
        for _ in range(_MAX_MEMORY_UNWRAPS):
            if candidate is None:
                return None
            found: object | None = getattr(candidate, "memory", None)
            if found is not None and callable(getattr(found, attribute, None)):
                return found
            candidate = getattr(candidate, "inner", None)
        return None

    def _submit_consume_step(
        self, host: NavigationHost, goal_id: str, drive: _ConsumeDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot serve the need honestly.
            drive.mission.mark_abandoned()
            self._finish_consume_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_consume_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_consume_complete(
        self, host: NavigationHost, goal_id: str, drive: _ConsumeDrive
    ) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _consume returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_consume_report(goal_id, drive)

    def _finish_consume_refused(
        self, host: NavigationHost, goal_id: str, drive: _ConsumeDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_consume_report(goal_id, drive)

    def _seal_consume_report(self, goal_id: str, drive: _ConsumeDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._consume_reports[goal_id] = drive.mission.report
        self._consume_reports.move_to_end(goal_id)
        while len(self._consume_reports) > MAX_KEPT_CONSUME_REPORTS:
            self._consume_reports.popitem(last=False)
        self._consumes.pop(goal_id, None)

    # -- care ----------------------------------------------------------------

    def _care(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one care mission, mirroring :meth:`_consume` join for join."""
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._cares.pop(goal_id, None)
            return None

        drive = self._cares.get(goal_id)
        if drive is None:
            mission = self._care_mission_for(goal_id, record)
            if mission is None:
                # Unreachable through the channel — the rest_until spec
                # requires the target — but a record is constructible without
                # it, and a rest with no target must refuse, not pick one.
                with host.goal_lock:
                    queue.fail(goal_id, ReasonCode.INVALID_ARGUMENT, NO_REST_TARGET_DETAIL)
                self._map.observe(observation)
                return None
            drive = _CareDrive(mission)
            self._cares[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The loot path's reasoning, verbatim: a step whose outcome was
            # never observed wedges the mission, so the goal ends typed.
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_care_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_care_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_care_complete(host, goal_id, drive)
            return None
        self._finish_care_refused(host, goal_id, drive, value)
        return None

    def _care_mission_for(self, goal_id: str, record: GoalRecord) -> _CareMission | None:
        """Build the mission the goal's own validated parameters describe.

        ``treat_wounds`` reads the wrapped chain's policy so the user's
        medical config (reserves, the dirty-dressing rule) decides the
        triage, exactly as the consume kinds read the food policy; the
        honest fallback is the default policy. ``rest_until``'s target and
        ``sleep_until_rested``'s hours are the record's own range-checked
        numbers — the channel ranges them with the adapters' bounds, so the
        constructors' own range guards cannot fire on a channel-admitted
        goal. ``None`` is the one gap a hand-built record can open: a
        rest_until with no target, refused by the caller.
        """
        if record.kind is GoalKind.REST_UNTIL:
            target = record.params.target_endurance
            if target is None:
                return None
            return RestMission(goal_id, target_endurance=target, limits=self._care_limits)
        if record.kind is GoalKind.SLEEP_UNTIL_RESTED:
            return SleepMission(goal_id, hours=record.params.hours, limits=self._care_limits)
        policy, _ = self._policy_ports()
        return TreatWoundsMission(goal_id, policy=policy, limits=self._care_limits)

    def _submit_care_step(
        self, host: NavigationHost, goal_id: str, drive: _CareDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot serve the need honestly.
            drive.mission.mark_abandoned()
            self._finish_care_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_care_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_care_complete(self, host: NavigationHost, goal_id: str, drive: _CareDrive) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _care returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_care_report(goal_id, drive)

    def _finish_care_refused(
        self, host: NavigationHost, goal_id: str, drive: _CareDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_care_report(goal_id, drive)

    def _seal_care_report(self, goal_id: str, drive: _CareDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._care_reports[goal_id] = drive.mission.report
        self._care_reports.move_to_end(goal_id)
        while len(self._care_reports) > MAX_KEPT_CARE_REPORTS:
            self._care_reports.popitem(last=False)
        self._cares.pop(goal_id, None)

    # -- avoid ---------------------------------------------------------------

    def _avoid(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one avoid mission, mirroring :meth:`_care` join for join."""
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._avoids.pop(goal_id, None)
            return None

        drive = self._avoids.get(goal_id)
        if drive is None:
            drive = _AvoidDrive(
                AvoidMission(
                    goal_id,
                    local_map=self._map,
                    safe_zones=self._known_safe_zones,
                    limits=self._avoid_limits,
                    journey_limits=self._limits,
                )
            )
            self._avoids[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The loot path's reasoning, verbatim, and this is the kind it
            # matters most for: a retreat that silently stopped retreating
            # while the threat closed is the worst ending in this stack.
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_avoid_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_avoid_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_avoid_complete(host, goal_id, drive)
            return None
        self._finish_avoid_refused(host, goal_id, drive, value)
        return None

    def _known_safe_zones(self) -> tuple[RememberedSafeZone, ...]:
        """The remembered user safe zones, through the same memory-port walk.

        Served by a ``safe_zones`` callable on the memory itself (a bare
        :class:`~pz_agent_core.memory.SaveMemory`) or on the one behind its
        ``loaded`` attribute (the sidecar memory), resolved *per call*
        because the sidecar memory re-loads when the save changes. With
        nothing wired the honest answer is the empty tuple, and the retreat
        falls back to open ground — distance, not a guess at where the user
        would have put a zone.
        """
        memory = self._memory_with("reserves_item")
        if memory is None:
            return ()
        records: object = None
        direct = getattr(memory, "safe_zones", None)
        if callable(direct):
            records = direct()
        else:
            loaded = getattr(memory, "loaded", None)
            if isinstance(loaded, SaveMemory):
                records = loaded.safe_zones()
        if not isinstance(records, tuple):
            return ()
        out: list[RememberedSafeZone] = []
        # Bounded by the memory store's own safe-zone ceiling; the slice
        # restates it so a foreign memory cannot make this walk unbounded.
        for record in records[:_MAX_KNOWN_SAFE_ZONES]:
            centre = getattr(record, "centre", None)
            radius = getattr(record, "radius", None)
            x = getattr(centre, "x", None)
            y = getattr(centre, "y", None)
            z = getattr(centre, "z", None)
            if not all(
                isinstance(value, int) and not isinstance(value, bool) for value in (x, y, z)
            ):
                continue
            if not isinstance(radius, int) or isinstance(radius, bool) or radius < 0:
                continue
            assert isinstance(x, int) and isinstance(y, int) and isinstance(z, int)
            out.append(RememberedSafeZone(centre=(x, y, z), radius=radius))
        return tuple(out)

    def _submit_avoid_step(
        self, host: NavigationHost, goal_id: str, drive: _AvoidDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot retreat honestly.
            drive.mission.mark_abandoned()
            self._finish_avoid_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_avoid_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_avoid_complete(
        self, host: NavigationHost, goal_id: str, drive: _AvoidDrive
    ) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _avoid returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_avoid_report(goal_id, drive)

    def _finish_avoid_refused(
        self, host: NavigationHost, goal_id: str, drive: _AvoidDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_avoid_report(goal_id, drive)

    def _seal_avoid_report(self, goal_id: str, drive: _AvoidDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._avoid_reports[goal_id] = drive.mission.report
        self._avoid_reports.move_to_end(goal_id)
        while len(self._avoid_reports) > MAX_KEPT_AVOID_REPORTS:
            self._avoid_reports.popitem(last=False)
        self._avoids.pop(goal_id, None)

    # -- combat --------------------------------------------------------------

    def _combat(self, goal_id: str, observation: Observation) -> ActionRequest | None:
        """Drive one combat mission, mirroring :meth:`_avoid` join for join.

        Every equip, shove, window and retreat travels the loop's action
        channel — served through the same gates and the same engine as
        everything else, which is precisely what lets the reflex guard and
        the stop levers interrupt between windows: each window is one
        ordinary channel command, and between commands nothing is running.
        """
        host = self._host
        if host is None or host.goals is None:
            # Unbound, or a loop that cannot activate goals at all: learn from
            # the observation and decline — ending goals is the queue's
            # privilege, and there is no queue.
            self._map.observe(observation)
            return None
        queue = host.goals
        self._prune()
        with host.goal_lock:
            record = queue.record(goal_id)
        if record is None or record.state is not GoalState.ACTIVE:
            self._map.observe(observation)
            if not _drive_lives(record):
                self._combats.pop(goal_id, None)
            return None

        drive = self._combats.get(goal_id)
        if drive is None:
            drive = _CombatDrive(EngageZombieMission(goal_id, limits=self._combat_limits))
            self._combats[goal_id] = drive
            self._enforce_cap()

        channel = host.actions
        pending = self._collect_mission_pending(drive, channel)
        if pending is _Pending.RUNNING:
            self._map.observe(observation)
            return None
        if pending is _Pending.LOST:
            # The loot path's reasoning, verbatim: a step whose outcome was
            # never observed wedges the mission, so the goal ends typed.
            self._map.observe(observation)
            drive.mission.mark_abandoned()
            self._finish_combat_refused(host, goal_id, drive, _lost_step_refusal())
            return None
        if (
            channel is not None
            and drive.pending_action_id is None
            and channel.pending_count >= channel.max_pending
        ):
            # Asking the mission for a step it could not submit would burn a
            # bound for nothing; learn from the observation and wait.
            self._map.observe(observation)
            return None

        value = drive.mission.next_step(observation)
        if value is None:
            return None
        if isinstance(value, MissionStep):
            self._submit_combat_step(host, goal_id, drive, value.request)
            return None
        if isinstance(value, MissionProbe):
            # The one goal-seam request a mission emits: its observed success
            # is what the loop settles the goal with, because no channel
            # result exists for a mission that never needed to act.
            return value.request
        if isinstance(value, MissionComplete):
            self._finish_combat_complete(host, goal_id, drive)
            return None
        self._finish_combat_refused(host, goal_id, drive, value)
        return None

    def _submit_combat_step(
        self, host: NavigationHost, goal_id: str, drive: _CombatDrive, request: ActionRequest
    ) -> None:
        channel = host.actions
        if channel is None:
            # Same reasoning as the journey path: a wrapper with no conveyor
            # for intermediate work cannot fight honestly — and a fight is
            # the last place to improvise a second dispatch route.
            drive.mission.mark_abandoned()
            self._finish_combat_refused(
                host,
                goal_id,
                drive,
                MissionRefused(
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    detail="the loop holds no action channel for the mission's steps",
                ),
            )
            return
        try:
            admitted = channel.submit(request)
        except LoopError:
            # Admission refused: the queue filled between the capacity check
            # and now -- the MCP router submits to this same channel from its
            # own thread -- or a restart made the key ambiguous. The mission
            # recorded this step as in flight when it emitted it and only a
            # terminal result clears that, so dropping it here would leave the
            # mission declining every later tick with none of its own bounds
            # advancing, until the goal's wall clock ended it as a timeout.
            # End it typed instead, exactly as an evicted record does.
            drive.mission.mark_abandoned()
            self._finish_combat_refused(host, goal_id, drive, _unadmitted_step_refusal())
            return
        drive.pending_action_id = admitted.action_id

    def _finish_combat_complete(
        self, host: NavigationHost, goal_id: str, drive: _CombatDrive
    ) -> None:
        """End a completed mission's goal on evidence something observed."""
        queue = host.goals
        assert queue is not None  # _combat returned before this without one
        last = drive.last_success
        if last is None:
            # Unreachable by construction — the mission answers Complete only
            # after a succeeded result — but a lie would be worse than a wait:
            # leave the goal to its budgets rather than fabricate evidence.
            return
        with host.goal_lock:
            queue.succeed(goal_id, last)
        self._seal_combat_report(goal_id, drive)

    def _finish_combat_refused(
        self, host: NavigationHost, goal_id: str, drive: _CombatDrive, refused: MissionRefused
    ) -> None:
        """End the goal with the mission's typed reason and summary line."""
        queue = host.goals
        if queue is None:
            return
        with host.goal_lock:
            queue.fail(goal_id, refused.reason_code, refused.detail)
        self._seal_combat_report(goal_id, drive)

    def _seal_combat_report(self, goal_id: str, drive: _CombatDrive) -> None:
        """Move the mission's report into the bounded ledger and drop the drive."""
        self._combat_reports[goal_id] = drive.mission.report
        self._combat_reports.move_to_end(goal_id)
        while len(self._combat_reports) > MAX_KEPT_COMBAT_REPORTS:
            self._combat_reports.popitem(last=False)
        self._combats.pop(goal_id, None)

    def _target_of(self, record: GoalRecord) -> NavigationTarget | None:
        params = record.params
        if params.target_x is None or params.target_y is None or params.target_z is None:
            return None
        return NavigationTarget(x=params.target_x, y=params.target_y, z=params.target_z)

    def _prune(self) -> None:
        """Drop every journey and mission whose goal will not run again.

        A pruned mission's report is sealed into the ledger first — the goal
        died under it (a cancel, a disarm, an expiry), and the partial report
        is exactly what the user who stopped it is owed.

        One survival besides ACTIVE: a goal parked with the queue's suspension
        marker (``suspended_by`` on a pending record) keeps its drive. That
        goal is coming back through ordinary activation once its preemptor
        ends, and the drive *is* the mid-mission position — the candidate
        list already swept, the legs already walked — that resumption exists
        to preserve. Dropping it would restart the mission from square one,
        which is exactly the cost the interrupt-and-resume design pays the
        suspension machinery to avoid.
        """
        host = self._host
        if host is None or host.goals is None:
            if self._journeys:
                self._journeys.clear()
            if self._returns:
                self._returns.clear()
            for goal_id in list(self._missions):
                self._abandon_mission(goal_id)
            for goal_id in list(self._explores):
                self._abandon_explore(goal_id)
            for goal_id in list(self._consumes):
                self._abandon_consume(goal_id)
            for goal_id in list(self._cares):
                self._abandon_care(goal_id)
            for goal_id in list(self._avoids):
                self._abandon_avoid(goal_id)
            for goal_id in list(self._combats):
                self._abandon_combat(goal_id)
            return
        queue = host.goals
        with host.goal_lock:
            dead = [
                goal_id for goal_id in self._journeys if not _drive_lives(queue.record(goal_id))
            ]
            dead_returns = [
                goal_id for goal_id in self._returns if not _drive_lives(queue.record(goal_id))
            ]
            dead_missions = [
                goal_id for goal_id in self._missions if not _drive_lives(queue.record(goal_id))
            ]
            dead_explores = [
                goal_id for goal_id in self._explores if not _drive_lives(queue.record(goal_id))
            ]
            dead_consumes = [
                goal_id for goal_id in self._consumes if not _drive_lives(queue.record(goal_id))
            ]
            dead_cares = [
                goal_id for goal_id in self._cares if not _drive_lives(queue.record(goal_id))
            ]
            dead_avoids = [
                goal_id for goal_id in self._avoids if not _drive_lives(queue.record(goal_id))
            ]
            dead_combats = [
                goal_id for goal_id in self._combats if not _drive_lives(queue.record(goal_id))
            ]
        for goal_id in dead:
            del self._journeys[goal_id]
        for goal_id in dead_returns:
            del self._returns[goal_id]
        for goal_id in dead_missions:
            self._abandon_mission(goal_id)
        for goal_id in dead_explores:
            self._abandon_explore(goal_id)
        for goal_id in dead_consumes:
            self._abandon_consume(goal_id)
        for goal_id in dead_cares:
            self._abandon_care(goal_id)
        for goal_id in dead_avoids:
            self._abandon_avoid(goal_id)
        for goal_id in dead_combats:
            self._abandon_combat(goal_id)
        self._enforce_cap()

    def _abandon_mission(self, goal_id: str) -> None:
        drive = self._missions.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_report(goal_id, drive)

    def _abandon_explore(self, goal_id: str) -> None:
        drive = self._explores.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_explore_report(goal_id, drive)

    def _abandon_consume(self, goal_id: str) -> None:
        drive = self._consumes.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_consume_report(goal_id, drive)

    def _abandon_care(self, goal_id: str) -> None:
        drive = self._cares.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_care_report(goal_id, drive)

    def _abandon_avoid(self, goal_id: str) -> None:
        drive = self._avoids.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_avoid_report(goal_id, drive)

    def _abandon_combat(self, goal_id: str) -> None:
        drive = self._combats.get(goal_id)
        if drive is None:
            return
        drive.mission.mark_abandoned()
        self._seal_combat_report(goal_id, drive)

    def _enforce_cap(self) -> None:
        while len(self._journeys) > MAX_TRACKED_JOURNEYS:
            self._journeys.popitem(last=False)
        while len(self._returns) > MAX_TRACKED_RETURNS:
            self._returns.popitem(last=False)
        while len(self._missions) > MAX_TRACKED_MISSIONS:
            goal_id, drive = self._missions.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            drive.mission.mark_abandoned()
            self._loot_reports[goal_id] = drive.mission.report
            self._loot_reports.move_to_end(goal_id)
            while len(self._loot_reports) > MAX_KEPT_LOOT_REPORTS:
                self._loot_reports.popitem(last=False)
        while len(self._explores) > MAX_TRACKED_EXPLORES:
            goal_id, evicted = self._explores.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            evicted.mission.mark_abandoned()
            self._explore_reports[goal_id] = evicted.mission.report
            self._explore_reports.move_to_end(goal_id)
            while len(self._explore_reports) > MAX_KEPT_EXPLORE_REPORTS:
                self._explore_reports.popitem(last=False)
        while len(self._consumes) > MAX_TRACKED_CONSUMES:
            goal_id, dropped = self._consumes.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            dropped.mission.mark_abandoned()
            self._consume_reports[goal_id] = dropped.mission.report
            self._consume_reports.move_to_end(goal_id)
            while len(self._consume_reports) > MAX_KEPT_CONSUME_REPORTS:
                self._consume_reports.popitem(last=False)
        while len(self._cares) > MAX_TRACKED_CARES:
            goal_id, shed = self._cares.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            shed.mission.mark_abandoned()
            self._care_reports[goal_id] = shed.mission.report
            self._care_reports.move_to_end(goal_id)
            while len(self._care_reports) > MAX_KEPT_CARE_REPORTS:
                self._care_reports.popitem(last=False)
        while len(self._avoids) > MAX_TRACKED_AVOIDS:
            goal_id, fled = self._avoids.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            fled.mission.mark_abandoned()
            self._avoid_reports[goal_id] = fled.mission.report
            self._avoid_reports.move_to_end(goal_id)
            while len(self._avoid_reports) > MAX_KEPT_AVOID_REPORTS:
                self._avoid_reports.popitem(last=False)
        while len(self._combats) > MAX_TRACKED_COMBATS:
            goal_id, fought = self._combats.popitem(last=False)
            # Evicted, not finished: the report is still owed to the ledger.
            fought.mission.mark_abandoned()
            self._combat_reports[goal_id] = fought.mission.report
            self._combat_reports.move_to_end(goal_id)
            while len(self._combat_reports) > MAX_KEPT_COMBAT_REPORTS:
                self._combat_reports.popitem(last=False)


def _unadmitted_step_refusal() -> MissionRefused:
    """The one refusal every kind ends an unadmitted channel step with.

    The mission recorded the step as in flight the moment it emitted it, and
    only a terminal result clears that; a request the channel never admitted
    has no record and will never produce one. Dropping it silently leaves the
    mission declining every later tick with none of its own bounds advancing,
    so the goal would end on its wall clock — ``ACTION_TIMEOUT`` minutes later,
    naming a timeout instead of the refusal that actually happened.
    """
    return MissionRefused(
        reason_code=ReasonCode.CAPABILITY_UNAVAILABLE, detail=UNADMITTED_STEP_DETAIL
    )


def _lost_step_refusal() -> MissionRefused:
    """The one refusal every kind ends a lost channel step with.

    ``CAPABILITY_UNAVAILABLE`` because that is the fact about this process:
    the channel that was carrying the mission's step can no longer report its
    outcome, exactly as a wrapper with no channel at all cannot carry one.
    One function so the six kinds cannot come to say it differently.
    """
    return MissionRefused(
        reason_code=ReasonCode.CAPABILITY_UNAVAILABLE, detail=UNOBSERVED_STEP_DETAIL
    )


def _drive_lives(record: GoalRecord | None) -> bool:
    """True while the drive serving *record*'s goal must be kept.

    ACTIVE is the ordinary case. The second case is the resumption promise:
    a pending record carrying the queue's suspension marker is a goal that
    stepped aside and is coming back, and its drive holds the mid-mission
    position resumption restores. Everything else — a forgotten id, a
    terminal record, a plain pending record (which can only mean the drive
    outlived one goal's end and another's admission) — is a drive to drop.
    """
    if record is None:
        return False
    if record.state is GoalState.ACTIVE:
        return True
    return record.state is GoalState.PENDING and record.suspended_by is not None


def _reason_of(error: object) -> tuple[ReasonCode, str]:
    """The protocol reason and the executor's failure token for one refusal.

    Budget exhaustions carry no protocol code by the executor's own design —
    "my budget ran out" is a statement about the executor, not the world — and
    the goal channel's honest word for that is ``NO_PROGRESS``: the budget was
    spent without the postcondition being observed.
    """
    if isinstance(error, NavigationError):
        reason = error.reason_code if error.reason_code is not None else ReasonCode.NO_PROGRESS
        return reason, error.failure.value
    # The wrapper's own refusals arrive as the small stand-in below.
    if isinstance(error, _WrapperRefusal):
        return error.reason, error.failure_detail
    return ReasonCode.NO_PROGRESS, "unknown"


class _WrapperRefusal:
    """A refusal minted by the wrapper itself, not by the executor."""

    __slots__ = ("failure_detail", "reason")

    def __init__(self, failure_detail: str, reason: ReasonCode) -> None:
        self.failure_detail = failure_detail
        self.reason = reason
