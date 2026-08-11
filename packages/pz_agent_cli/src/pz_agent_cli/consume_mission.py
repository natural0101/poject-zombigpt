"""``satisfy_hunger`` / ``satisfy_thirst``: the mandatory chain, deterministic.

The two consumption kinds were the goal channel's founding members and, until
this module, the only deterministic-by-vocation kinds still served through a
plan provider. The chain they exist for is fixed: need detected → nothing safe
carried → find the need's category in reachable containers → filter what the
safety policy refuses (rotten, burnt, poisoned, the user's reserves) → move
one item to the main inventory → consume a reasonable portion → confirm the
stat moved → hand the loop back. Every decision on that path is made here and
in the selection policies; the model (or the microphone) said "I am hungry",
and no model is consulted after that token.

The mission is a state machine over observations, shaped exactly like
:class:`~pz_agent_cli.loot_mission.LootMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` drives all of them through one seam.
It answers with the *same* frozen move vocabulary, imported rather than
restated, so the wrapper cannot come to route the missions' moves differently:
:class:`~pz_agent_cli.loot_mission.MissionStep` travels the loop's action
channel (approach legs, opens, inspects, transfers, the consume itself — no
single step's success is by itself the goal's postcondition, the *verified*
stat movement is), :class:`~pz_agent_cli.loot_mission.MissionProbe` travels
the goal seam only for a mission that finished without one action running (the
need was already at its target when the goal activated — the same
already-there arrangement navigation and loot use), and
:class:`~pz_agent_cli.loot_mission.MissionComplete` /
:class:`~pz_agent_cli.loot_mission.MissionRefused` end the goal through the
queue on real evidence or a typed reason.

Honesty rules this module keeps:

* **The sandwich is picked by the selection policies, never here.** The
  candidate for each attempt comes from
  :func:`~pz_agent_core.policy.food.select_food` /
  :func:`~pz_agent_core.policy.drink.select_drink` — the one place the
  rotten/burnt/poison/raw/tainted gates live — over a pre-filtered view. The
  pre-filter is deliberately dumb: the need's loot category
  (:func:`~pz_agent_core.loot.classify` — FOOD feeds hunger, WATER thirst),
  the user's reserves, and the refs this mission already spent. Restating any
  safety gate here would be the second copy of the filters the policy modules
  exist to prevent, so none is restated. The *order* among safe candidates is
  therefore the policy's own deterministic ranking (score, ties broken through
  to the item reference), not a rule of this module's.
* **The user's reserve outranks hunger.** An item type the save's memory
  reserves (:meth:`~pz_agent_core.memory.SaveMemory.reserves_item`) is removed
  before the policy ever sees it, at any hunger — the agent walks to a
  container, or fails typed, before it eats the user's strategic reserve.
  That is the loot policy's own precedence ("the user's word outranks the
  flag"), applied to the mouth as well as the bag, and a test pins it.
* **Absent is never zero.** The stat is read from the observation by name; a
  build that reports no ``hunger`` gets a typed ``PRECONDITION_FAILED``, not
  a mission that reads the missing stat as satisfied.
* **Consumed means the need was observed moving.** The consume adapters
  verify stat-or-portion before the engine calls the action succeeded; the
  mission then *additionally* reads the need from a fresh observation, and its
  own completion criterion is the stat at-or-below the target — or moved
  toward it with nothing safe left to spend. A bite the world swallowed
  without the reading moving is recorded, never counted.
* **Bounded everything.** Candidates per mission (carried attempts and
  container visits share one budget), fetch radius, consecutive failures,
  completion probes, sightings remembered — each has a constant here, every
  sub-action has its adapter's own budgets, and the goal's wall clock sits
  over all of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from pz_agent_core.actions.adapters.common import world_square
from pz_agent_core.actions.adapters.movement import (
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
    MOVE_RETRY_POLICY,
)
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.loot import LootCategory, classify
from pz_agent_core.memory import MemoryValueError, container_tail
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
from pz_agent_core.policy.config import PolicyConfig
from pz_agent_core.policy.drink import DrinkChoice, select_drink
from pz_agent_core.policy.food import FoodChoice, select_food
from pz_agent_core.policy.selection import NO_CAPABILITIES, CapabilityLookup
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    ContainerRef,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
    ReasonCode,
)

from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    IsReserved,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "CONSUME_PHASES",
    "ENDED_NOTHING_FOUND",
    "ENDED_UNREPORTED",
    "HUNGER",
    "MAX_CANDIDATES_PER_MISSION",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_DISCOVERIES_PER_OBSERVATION",
    "MAX_FETCH_DISTANCE",
    "MAX_TRACKED_SIGHTINGS",
    "PHASE_CHECK",
    "PHASE_CONSUME",
    "PHASE_FETCH",
    "PHASE_VERIFY",
    "THIRST",
    "ConsumeMission",
    "ConsumeMissionLimits",
    "KnownContainers",
    "NeedSpec",
    "RememberedContainer",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Candidates one mission will ever spend, counting both carried consume
#: attempts and world containers visited — they serve the one need, so they
#: share the one budget. Deliberately smaller than the loot mission's sweep
#: bound: "eat something" that is still hunting after eight tries is a supply
#: problem to report, not a patrol to continue.
MAX_CANDIDATES_PER_MISSION: Final = 8

#: How far from the character's square a *remembered* container may be and
#: still be walked to for one meal, in Chebyshev squares. The single-move
#: ceiling the channel already admits (``MAX_MOVE_DISTANCE_SQUARES``): a
#: longer trip for food is a journey the user states as its own goal.
MAX_FETCH_DISTANCE: Final = 30

#: Consecutive sub-action failures (opens, inspects, transfers, stuck
#: journeys — anything that is not a typed refusal about the world or the
#: item) before the mission ends ``NO_PROGRESS``. The loot mission's number,
#: restated because the missions may diverge and a shared constant would hide
#: the review when one does.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no action may spend, exactly the loot
#: mission's arrangement: each probe is one goal-seam request, so the goal's
#: own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: Nearby objects scanned per observation, mirroring the loot mission's own
#: sighting bound for the same reason: the mod bounds the tier, and this seam
#: stays bounded by its own number rather than by trust in the sender's.
MAX_DISCOVERIES_PER_OBSERVATION: Final = 64

#: World-container sightings the mission remembers across observations, so a
#: remembered candidate can be resolved back to a ref after a walk. The cap is
#: the memory store's own container ceiling's shape at mission scale; past it
#: new sightings are simply not remembered, which costs a candidate, never
#: unbounded growth.
MAX_TRACKED_SIGHTINGS: Final = 128

#: Inside this distance ``container.open_nearby`` is itself the walk, exactly
#: as the loot mission documents; beyond it (or across floors) a Journey is
#: routed first.
_APPROACH_DISTANCE: Final = MAX_MOVE_DISTANCE_SQUARES

#: The arrival radius an approach journey aims for — the loot mission's own
#: reasoning: the journey only needs to bring the container within
#: ``open_nearby``'s reach, and the container's square is frequently the one
#: square the character cannot occupy.
_APPROACH_RADIUS: Final = MAX_ARRIVAL_RADIUS


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

#: Every reachable candidate was spent and nothing safe was ever consumable.
ENDED_NOTHING_FOUND: Final = "nothing_found"

#: The build does not report the stat this mission exists to move.
ENDED_UNREPORTED: Final = "unreported"

#: Typed refusals that are facts about the item or the world, not the mission
#: failing to make progress: they cost a recorded skip, never a failure count,
#: exactly as a locked door does in the loot mission.
_TYPED_ITEM_REFUSALS: Final = frozenset(
    {
        ReasonCode.NO_SAFE_FOOD,
        ReasonCode.NO_SAFE_DRINK,
        ReasonCode.PRECONDITION_FAILED,
        ReasonCode.INVALID_ARGUMENT,
        ReasonCode.POLICY_DENIED,
    }
)

#: The public pipeline phases, in the order the chain runs them. Closed tokens
#: minted here — never caller or game text — so the progress surface may
#: publish them verbatim. ``check`` covers "reading the need and choosing what
#: is carried", ``fetch`` the whole container pipeline (approach, open,
#: inspect, transfer, and a bring-to-hand move), ``consume`` an eat or drink
#: in flight, ``verify`` a consume whose fresh stat reading is still owed.
PHASE_CHECK: Final = "check"
PHASE_FETCH: Final = "fetch"
PHASE_CONSUME: Final = "consume"
PHASE_VERIFY: Final = "verify"

#: The closed set, in pipeline order, for tests and documentation to pin.
CONSUME_PHASES: Final = (PHASE_CHECK, PHASE_FETCH, PHASE_CONSUME, PHASE_VERIFY)

#: Internal stages. The public :attr:`ConsumeMission.phase` is a projection of
#: these; they exist because the fetch pipeline has more joints than the
#: progress surface needs to see.
_S_CHECK: Final = "check"
_S_APPROACH: Final = "approach"
_S_OPEN: Final = "open"
_S_INSPECT: Final = "inspect"
_S_TRANSFER: Final = "transfer"
_S_ENSURE: Final = "ensure"
_S_CONSUME: Final = "consume"

_FETCH_STAGES: Final = frozenset({_S_APPROACH, _S_OPEN, _S_INSPECT, _S_TRANSFER, _S_ENSURE})


# --------------------------------------------------------------------------
# the two needs, and the ports
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NeedSpec:
    """One need the mission can serve: which stat, which category, which verb.

    Two instances exist (:data:`HUNGER`, :data:`THIRST`) and the whole mission
    is parameterised by one of them — one state machine, because the chain is
    the same chain and two copies of it would drift.
    """

    stat: str
    category: LootCategory
    action: ActionName
    refusal: ReasonCode


HUNGER: Final = NeedSpec(
    stat="hunger",
    category=LootCategory.FOOD,
    action=ActionName.CONSUME_EAT,
    refusal=ReasonCode.NO_SAFE_FOOD,
)

THIRST: Final = NeedSpec(
    stat="thirst",
    category=LootCategory.WATER,
    action=ActionName.CONSUME_DRINK,
    refusal=ReasonCode.NO_SAFE_DRINK,
)


@dataclass(frozen=True, slots=True)
class RememberedContainer:
    """As much of one :class:`~pz_agent_core.memory.KnownContainer` as fetching reads.

    Minted here rather than importing the memory model so the port stays a
    value the wrapper can build from *any* memory it actually holds — the
    shipped sidecar store, a test double, or nothing at all.
    """

    tail: str
    square: GridSquare | None
    categories: frozenset[str]
    inspected: bool


#: The container-knowledge port: what the save's memory remembers, re-read per
#: call because the sidecar memory re-loads when the save changes. The honest
#: answer with nothing wired is the empty tuple — the mission then fetches
#: only from what it can see, which is what "no memory" truthfully affords.
KnownContainers = Callable[[], tuple[RememberedContainer, ...]]

#: What either selection policy hands back; the four fields the mission reads
#: are common to both.
_Choice = FoodChoice | DrinkChoice


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsumeMissionLimits:
    """Every bound one mission runs under. All of them are load-bearing."""

    max_candidates: int = MAX_CANDIDATES_PER_MISSION
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        for name in ("max_candidates", "max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Fetch:
    """One world container the mission is fetching from, and where it stands."""

    tail: str
    ref: str
    square: GridSquare
    index: int


@dataclass(frozen=True, slots=True)
class _PendingConsume:
    """One consume in flight: what was bitten, and the reading to beat."""

    item_ref: str
    item: str
    fraction: float
    stat_before: float


def _chebyshev(a: GridSquare, b: GridSquare) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _reported_stat(observation: Observation, key: str) -> float | None:
    """The stat as a number, or None when the mod did not report it.

    Read by name from the open stats map — deliberately not through the
    :class:`~pz_agent_core.protocol.PlayerState` property, whose default reads
    an absent stat as zero. "This build does not report hunger" and "the
    character is not hungry" are different facts, and only the survival
    adapters' spelling of the read keeps them apart.
    """
    if key not in observation.player.stats:
        return None
    value = observation.player.stats[key]
    if value is None or isinstance(value, bool):
        return None
    return float(value)


def _main_ref(observation: Observation) -> str:
    return str(ContainerRef.player_main(observation.session_id))


def _door_named(reason: ReasonCode | None, door_ref: str | None) -> str | None:
    """A door-shaped skip reason, or None when the refusal is not one."""
    if reason is ReasonCode.DOOR_LOCKED:
        return f"door {door_ref or 'unknown'} is locked"
    if reason is ReasonCode.DOOR_BARRICADED:
        return f"door {door_ref or 'unknown'} is barricaded"
    return None


class ConsumeMission:
    """One ``satisfy_hunger`` or ``satisfy_thirst`` goal, driven bite by bite.

    Created by the wrapper when the goal activates and discarded when the goal
    reaches a terminal state — the mission never outlives its goal. All inputs
    are injected and deterministic: the shared
    :class:`~pz_agent_core.navigation.LocalMap`, the frozen
    :class:`~pz_agent_core.policy.config.PolicyConfig` the selection policies
    score under, the capability lookup that decides whether a fraction may be
    less than the whole item, and the two memory ports.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        need: NeedSpec,
        local_map: LocalMap,
        policy: PolicyConfig,
        is_reserved: IsReserved,
        known_containers: KnownContainers,
        satisfy_to: float | None = None,
        capabilities: CapabilityLookup = NO_CAPABILITIES,
        limits: ConsumeMissionLimits | None = None,
        journey_limits: JourneyLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._key_stem = goal_id[:MAX_JOURNEY_ID_LEN]
        self._need = need
        self._map = local_map
        self._policy = policy
        self._is_reserved = is_reserved
        self._known_containers = known_containers
        # The submitted satisfy_to outranks the policy default: the parameter
        # is the user's own number, validated 0..1 at the channel door.
        self._target = (
            satisfy_to
            if satisfy_to is not None
            else (policy.satisfied_hunger if need.stat == "hunger" else policy.satisfied_thirst)
        )
        self._capabilities = capabilities
        self._limits = limits if limits is not None else ConsumeMissionLimits()
        self._journey_limits = journey_limits if journey_limits is not None else JourneyLimits()

        self._stage = _S_CHECK
        self._pending_action: str | None = None
        self._journey: Journey | None = None
        self._current: _Fetch | None = None
        self._await: _PendingConsume | None = None
        self._ensure_ref: str | None = None
        self._verify_due = False

        self._steps = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False
        self._candidates_spent = 0
        self._fetch_index = 0
        self._tried_item_refs: set[str] = set()
        self._reserved_refs: set[str] = set()
        self._visited_tails: set[str] = set()
        self._seen_nearby: dict[str, tuple[str, GridSquare]] = {}

        self._stat_start: float | None = None
        self._stat_last: float | None = None
        self._moved = False
        self._consumed: list[tuple[str, float]] = []
        self._skipped: list[tuple[str, str]] = []
        self._containers_visited: list[str] = []
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
    def any_success(self) -> bool:
        """Whether any channel action this mission asked for succeeded."""
        return self._any_success

    @property
    def phase(self) -> str:
        """Where the chain stands: one of :data:`CONSUME_PHASES`.

        Closed tokens minted by this module, never caller or game text, so a
        status surface may publish the value verbatim. A mission that already
        ended keeps the phase it stopped in — whether it ended is
        :attr:`ended`'s answer, not this one's.
        """
        if self._verify_due:
            return PHASE_VERIFY
        if self._stage == _S_CONSUME:
            return PHASE_CONSUME
        if self._stage in _FETCH_STAGES:
            return PHASE_FETCH
        return PHASE_CHECK

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

        Item names came out of the game, so they pass through
        :func:`~pz_agent_core.observation.compact.redact_text` like every
        other game-authored string this project renders; refs and reason
        tokens are this side's own vocabulary.
        """
        return {
            "need": self._need.stat,
            "target": self._target,
            "stat_before": self._stat_start,
            "stat_after": self._stat_last,
            "consumed": [{"item": item, "fraction": fraction} for item, fraction in self._consumed],
            "candidates_tried": self._candidates_spent,
            "reserved_withheld": len(self._reserved_refs),
            "containers_visited": list(self._containers_visited),
            "skipped": [{"ref": ref, "reason": reason} for ref, reason in self._skipped],
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line of the report, for a goal record's ``detail``.

        Constants and counts only — no refs, no game-authored names — so it
        satisfies the goal channel's rule that a detail is never caller text.
        """
        return (
            f"{self._need.stat} {self._ended}: consumed={len(self._consumed)} "
            f"tried={self._candidates_spent} skipped={len(self._skipped)}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Non-terminal acks and results for actions this mission did not ask
        for are ignored — the mission has at most one step in flight.
        """
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        if self._stage == _S_APPROACH:
            journey = self._journey
            if journey is not None:
                journey.note_result(result)
            if result.status is ActionStatus.SUCCEEDED:
                self._note_success()
            return
        if self._stage == _S_CONSUME:
            self._note_consume_result(result)
            return
        if self._stage == _S_ENSURE:
            self._note_ensure_result(result)
            return
        if result.status is ActionStatus.SUCCEEDED:
            self._note_success()
            if self._stage == _S_OPEN:
                self._stage = _S_INSPECT
            elif self._stage == _S_INSPECT:
                self._stage = _S_TRANSFER
            elif self._stage == _S_TRANSFER:
                self._note_transfer_landed()
            return
        self._note_fetch_failure(result)

    def _note_consume_result(self, result: ActionResult) -> None:
        pending = self._await
        self._stage = _S_CHECK
        if result.status is ActionStatus.SUCCEEDED:
            self._note_success()
            # The adapter verified stat-or-portion; the mission still owes its
            # own reading from the next fresh observation.
            self._verify_due = True
            return
        self._await = None
        reason = result.reason_code
        if pending is not None:
            # A refused candidate is spent, whatever the reason: the same
            # reference would be refused again the same way.
            self._tried_item_refs.add(pending.item_ref)
            self._skipped.append(
                (pending.item_ref, f"{result.action} refused: {self._reason_token(reason)}")
            )
        if reason not in _TYPED_ITEM_REFUSALS:
            self._count_failure()

    def _note_ensure_result(self, result: ActionResult) -> None:
        self._stage = _S_CHECK
        moved_ref = self._ensure_ref
        self._ensure_ref = None
        if result.status is ActionStatus.SUCCEEDED:
            # The item is observed in the main inventory on the next tick,
            # where the carried path re-selects it — deliberately re-selects,
            # because the world may have moved while the hands did.
            self._note_success()
            return
        if moved_ref is not None:
            self._tried_item_refs.add(moved_ref)
            self._skipped.append(
                (moved_ref, f"{result.action} refused: {self._reason_token(result.reason_code)}")
            )
        if result.reason_code not in _TYPED_ITEM_REFUSALS:
            self._count_failure()

    def _note_transfer_landed(self) -> None:
        """The pick was observed in the main inventory; the visit paid off."""
        current = self._current
        if current is not None:
            self._containers_visited.append(current.ref)
        self._reset_fetch()

    def _note_fetch_failure(self, result: ActionResult) -> None:
        reason = result.reason_code
        door = _door_named(reason, self._door_ref_of(result))
        if door is not None:
            # A sealed door is a typed fact about the world; it costs no
            # failure, exactly the loot mission's rule.
            self._skip_fetch(door)
            return
        self._count_failure()
        self._skip_fetch(f"{result.action} failed: {self._reason_token(reason)}")

    def _reason_token(self, reason: ReasonCode | None) -> str:
        return reason.value if reason is not None else "UNKNOWN"

    @staticmethod
    def _door_ref_of(result: ActionResult) -> str | None:
        door_ref = result.evidence.get("door_ref")
        return door_ref if isinstance(door_ref, str) else None

    def _note_success(self) -> None:
        self._any_success = True
        self._consecutive_failures = 0

    def _count_failure(self) -> None:
        self._consecutive_failures += 1

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: learn the map and the sightings, read the need (absent
        is a typed refusal, never zero), settle a consume's owed verification,
        finish if the target holds, then carried candidates before fetches — or
        finish empty-handed. A finished mission replays its terminal value.
        """
        if self._final is not None:
            return self._final
        self._map.observe(observation)
        self._discover(observation)
        if self._pending_action is not None:
            # A channel step is still being driven; the mission decides on the
            # observation after its result lands.
            return None
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive sub-actions failed",
            )
        stat = _reported_stat(observation, self._need.stat)
        if stat is None:
            return self._refuse(
                ENDED_UNREPORTED,
                ReasonCode.PRECONDITION_FAILED,
                f"this build does not report {self._need.stat}",
            )
        if self._stat_start is None:
            self._stat_start = stat
        self._stat_last = stat
        if self._verify_due:
            self._settle_verify(stat)
        if stat <= self._target:
            return self._finish_satisfied(observation)
        if observation.inventory is None:
            # No inventory tier this tick: nothing carried is provable and a
            # fetch's inspect would be blind too. Wait for an observation that
            # can be acted on; the goal's own budgets bound the wait.
            return None
        if self._candidates_spent >= self._limits.max_candidates:
            return self._finish_exhausted()
        if self._current is None and self._stage == _S_CHECK:
            move = self._consume_carried(observation, stat)
            if move is not None:
                return move
        # Bounded by the candidate budget: each iteration either emits a
        # request, records a skip and drops the candidate, advances a stage
        # on an already-observed fact (an arrival noticed mid-drive), or
        # runs out of candidates and finishes.
        while True:
            if self._current is None:
                self._start_next_fetch(observation)
                if self._current is None:
                    return self._finish_exhausted()
            move = self._drive_fetch(observation)
            if move is not None:
                return move

    # -- verification --------------------------------------------------------

    def _settle_verify(self, stat: float) -> None:
        """Fold the fresh reading into the consume the adapter already blessed."""
        pending = self._await
        self._await = None
        self._verify_due = False
        if pending is None:
            return
        if stat < pending.stat_before:
            self._moved = True
            self._consumed.append((pending.item, pending.fraction))
            return
        # The adapter's evidence was the item's own counter (a bite too small
        # to move the stat). Recorded, not counted: this mission's currency is
        # the need, and the need did not move.
        self._tried_item_refs.add(pending.item_ref)
        self._skipped.append(
            (pending.item_ref, f"consumed but the {self._need.stat} reading did not move")
        )

    # -- carried first -------------------------------------------------------

    def _prefilter(self, items: list[ItemView]) -> list[ItemView]:
        """Category, reserves and spent refs — and deliberately nothing else.

        Rot, burn, poison and the rest stay the selection policies' gates;
        restating one here would be the second copy of the filters that no
        test compares against the first.
        """
        kept: list[ItemView] = []
        for item in items:
            if classify(item) is not self._need.category:
                continue
            if item.ref in self._tried_item_refs:
                continue
            if self._is_reserved(item.full_type):
                # The user's word outranks the need, at any hunger: the
                # mission fetches, or fails typed, before it opens these.
                self._reserved_refs.add(item.ref)
                continue
            kept.append(item)
        return kept

    def _select(
        self, view: InventoryView, observation: Observation, *, world: bool
    ) -> _Choice | None:
        """The policies' verdict over *view* — the one place the sandwich is picked."""
        config = replace(self._policy, allow_world_containers=True) if world else self._policy
        if self._need.stat == "hunger":
            return select_food(
                view, observation.player, config, capabilities=self._capabilities
            ).choice
        return select_drink(
            view, observation.player, config, capabilities=self._capabilities
        ).choice

    def _consume_carried(self, observation: Observation, stat: float) -> NextMissionMove:
        inventory = observation.inventory
        if inventory is None:
            # No inventory tier this tick: nothing carried is *provable*, and
            # the fetch pipeline's inspect would be blind too — wait for the
            # next observation rather than guess.
            return None
        items = self._prefilter(list(inventory.items))
        if not items:
            return None
        view = InventoryView(containers=list(inventory.containers), items=items)
        choice = self._select(view, observation, world=False)
        if choice is None:
            return None
        if choice.needs_transfer_to_main:
            # §4.7's dependency, honoured as its own observable step rather
            # than folded into the consume: the adapter would refuse the
            # combined command, and rightly.
            self._stage = _S_ENSURE
            self._ensure_ref = choice.item_ref
            return self._emit(
                observation, ActionName.INVENTORY_ENSURE_MAIN, {"item_ref": choice.item_ref}
            )
        self._stage = _S_CONSUME
        self._candidates_spent += 1
        self._await = _PendingConsume(
            item_ref=choice.item_ref,
            item=redact_text(choice.display_name),
            fraction=choice.fraction,
            stat_before=stat,
        )
        return self._emit(
            observation,
            self._need.action,
            {"item_ref": choice.item_ref, "fraction": choice.fraction},
        )

    # -- fetching ------------------------------------------------------------

    def _discover(self, observation: Observation) -> None:
        nearby = observation.nearby
        if nearby is None:
            return
        for seen in nearby.objects[:MAX_DISCOVERIES_PER_OBSERVATION]:
            square = world_square(seen.ref)
            if square is None:
                # Doors, windows, corpses, player containers: ordinary
                # neighbours, not candidates.
                continue
            try:
                tail = container_tail(seen.ref)
            except MemoryValueError:
                continue
            if tail not in self._seen_nearby and len(self._seen_nearby) >= MAX_TRACKED_SIGHTINGS:
                continue
            self._seen_nearby[tail] = (seen.ref, square)

    def _category_known(self, record: RememberedContainer) -> bool:
        wanted = self._need.category.value.casefold()
        return any(category.casefold() == wanted for category in record.categories)

    def _start_next_fetch(self, observation: Observation) -> None:
        """Pick the next container candidate, deterministically, or none.

        Three tiers, in the chain's own preference order — (0) observed
        containers whose remembered categories include the need's, (1)
        remembered containers with the category within the fetch radius, (2)
        observed containers never yet inspected, as the last resort — each
        tier nearest-first, ties broken on the tail. A container memory has
        inspected and filed under other categories only is not a candidate:
        the memory's word is exactly what spares the trip.
        """
        if self._candidates_spent >= self._limits.max_candidates:
            return
        here = square_of(observation.player.position)
        remembered = {record.tail: record for record in self._known_containers()}
        candidates: list[tuple[int, int, str, str, GridSquare]] = []
        for tail, (ref, square) in self._seen_nearby.items():
            if tail in self._visited_tails:
                continue
            record = remembered.get(tail)
            distance = _chebyshev(here, square)
            if record is not None and self._category_known(record):
                candidates.append((0, distance, tail, ref, square))
            elif record is None or not record.inspected:
                candidates.append((2, distance, tail, ref, square))
        for tail, record in remembered.items():
            if tail in self._visited_tails or tail in self._seen_nearby:
                continue
            if record.square is None or not self._category_known(record):
                continue
            distance = _chebyshev(here, record.square)
            if distance > MAX_FETCH_DISTANCE:
                continue
            candidates.append((1, distance, tail, "", record.square))
        if not candidates:
            return
        _tier, _distance, tail, ref, square = min(candidates)
        self._fetch_index += 1
        self._candidates_spent += 1
        self._visited_tails.add(tail)
        self._current = _Fetch(tail=tail, ref=ref, square=square, index=self._fetch_index)
        self._stage = _S_APPROACH if self._needs_journey(here, square) else _S_OPEN
        if self._stage == _S_APPROACH:
            self._journey = Journey(
                self._map,
                NavigationTarget(x=square[0], y=square[1], z=square[2], radius=_APPROACH_RADIUS),
                journey_id=f"{self._key_stem[: MAX_JOURNEY_ID_LEN - 8]}.f{self._fetch_index}",
                limits=self._journey_limits,
            )

    @staticmethod
    def _needs_journey(here: GridSquare, target: GridSquare) -> bool:
        return _chebyshev(here, target) > _APPROACH_DISTANCE or here[2] != target[2]

    def _drive_fetch(self, observation: Observation) -> NextMissionMove:
        candidate = self._current
        assert candidate is not None  # the caller just placed one
        if self._stage == _S_APPROACH:
            return self._drive_journey(observation)
        if not candidate.ref:
            resolved = self._seen_nearby.get(candidate.tail)
            if resolved is None:
                # Walked to the remembered square and nothing there describes
                # the container: the memory was stale, said out loud.
                self._skip_fetch("the remembered container was not observed at its square")
                return None
            candidate.ref = resolved[0]
        if self._stage == _S_OPEN:
            return self._emit(
                observation, ActionName.CONTAINER_OPEN_NEARBY, {"container_ref": candidate.ref}
            )
        if self._stage == _S_INSPECT:
            return self._emit(
                observation, ActionName.CONTAINER_INSPECT, {"container_ref": candidate.ref}
            )
        return self._drive_transfer(observation, candidate)

    def _drive_journey(self, observation: Observation) -> NextMissionMove:
        journey = self._journey
        assert journey is not None  # set beside the APPROACH stage
        value = journey.next_step(observation)
        if isinstance(value, Arrived):
            self._journey = None
            self._stage = _S_OPEN
            return None
        if isinstance(value, Refused):
            self._journey = None
            self._skip_after_journey(value.error)
            return None
        step: Step = value
        self._pending_action = step.request.action.value
        return MissionStep(request=step.request)

    def _skip_after_journey(self, error: NavigationError) -> None:
        door = _door_named(error.reason_code, error.door_ref)
        if door is not None:
            self._skip_fetch(door)
            return
        if error.reason_code is ReasonCode.PATH_NOT_FOUND:
            # A proven wall is a fact about the world like a locked door; it
            # costs a skip, not a failure.
            self._skip_fetch(f"approach refused: {error.failure.value}")
            return
        self._count_failure()
        self._skip_fetch(f"approach refused: {error.failure.value}")

    def _drive_transfer(self, observation: Observation, candidate: _Fetch) -> NextMissionMove:
        inventory = observation.inventory
        described = None if inventory is None else inventory.container(candidate.ref)
        if inventory is None or described is None:
            self._count_failure()
            self._skip_fetch("the observation does not describe the container")
            return None
        items = self._prefilter(inventory.items_in(candidate.ref))
        choice = (
            self._select(
                InventoryView(containers=[described], items=items), observation, world=True
            )
            if items
            else None
        )
        if choice is None:
            self._skip_fetch(f"no safe {self._need.category.value} inside")
            return None
        # A single transfer on purpose: the mission is feeding a need, not
        # looting — one safe item to hand, and the loot mission owns hauls.
        return self._emit(
            observation,
            ActionName.INVENTORY_TRANSFER,
            {
                "item_ref": choice.item_ref,
                "destination_container_ref": _main_ref(observation),
            },
        )

    def _skip_fetch(self, reason: str) -> None:
        current = self._current
        if current is not None:
            self._skipped.append((current.ref or current.tail, reason))
        self._reset_fetch()

    def _reset_fetch(self) -> None:
        self._current = None
        self._journey = None
        self._stage = _S_CHECK

    # -- endings -------------------------------------------------------------

    def _finish_satisfied(self, observation: Observation) -> NextMissionMove:
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
        # At or below the target with no action ever run: nothing exists to
        # succeed the goal with, and minting a result would fabricate
        # evidence. The probe is the engine observing an ordinary fact —
        # the goal-seam arrangement navigation and loot both use. Numbered so
        # a failed probe's retry never reuses a key.
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"consume:{self._key_stem}:done{self._probes}",
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

    def _finish_exhausted(self) -> NextMissionMove:
        if self._moved and self._any_success:
            # The need was observed moving toward the target and nothing safe
            # is left to spend: that is the chain's honest best, reported as
            # complete with the readings in the report saying how far it got.
            self._ended = ENDED_COMPLETE
            self._final = MissionComplete()
            return self._final
        if self._consumed or any(reason.startswith("consumed") for _, reason in self._skipped):
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"items were consumed but the {self._need.stat} reading never moved",
            )
        return self._refuse(
            ENDED_NOTHING_FOUND,
            self._need.refusal,
            f"no safe {self._need.category.value} was found in "
            f"{len(self._visited_tails)} reachable containers",
        )

    def _refuse(self, ended: str, reason_code: ReasonCode, cause: str) -> MissionRefused:
        self._ended = ended
        refused = MissionRefused(reason_code=reason_code, detail=f"{cause}; {self.summary_line()}")
        self._final = refused
        return refused

    # -- emission ------------------------------------------------------------

    def _emit(self, observation: Observation, action: ActionName, args: JsonDict) -> MissionStep:
        self._steps += 1
        request = ActionRequest(
            action=action,
            session_id=observation.session_id,
            idempotency_key=f"consume:{self._key_stem}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)
