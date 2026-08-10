"""``loot_area``: a deterministic mission over navigation, inspection and transfer.

The goal channel gained a second kind no plan provider serves. Where
``navigate_to`` is one journey, ``loot_area`` is a *mission*: build the
reachable candidate map, walk to each container, open it, read what it holds,
let the deterministic loot policy pick, move the picks with one bounded batch,
and finish on a provable criterion — **every reachable container in scope was
inspected or carries a recorded skip reason**. No model call anywhere on the
path: the model (or the microphone) said "loot the area"; everything after
that token is decided here and in :mod:`pz_agent_core.loot`.

The mission is a state machine over observations, shaped exactly like
:class:`~pz_agent_core.navigation.Journey` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` can drive both through one seam:
:meth:`LootMission.next_step` folds the fresh observation in and answers with
one frozen value, and every terminal engine result is reported back through
:meth:`LootMission.note_result` before the next decision. Which route each
value takes is the wrapper's half of the contract:

* :class:`MissionStep` travels the loop's **action channel** — approach legs,
  door retries, ``container.open_nearby``, ``container.inspect``,
  ``inventory.transfer_batch``. None of those steps' successes is the goal's
  postcondition, so none of them may end the goal.
* :class:`MissionProbe` travels the **goal seam** and is emitted only for a
  mission that finished without a single action running (every candidate
  skipped from memory, or no candidate at all). The queue's one route to
  ``SUCCEEDED`` needs a real engine result with observed evidence, and this
  module refuses to mint one — the probe is a bounded move inside the square
  the character already stands on, whose *observed* success the loop settles
  the goal with, exactly the arrangement navigation uses for "already there".
* :class:`MissionComplete` and :class:`MissionRefused` end the goal through
  the queue — success on the last real channel result's evidence, refusal
  with a typed reason and a one-line report summary.

Honesty rules this module keeps:

* **Scope is pinned from an observation, never guessed.** ``scope=room`` or
  ``building`` with the character's own room unreadable is a typed
  ``PRECONDITION_FAILED`` naming ``scope=radius`` as the alternative — the
  protocol is explicit that ``None`` covers both "outdoors" and "no reader on
  this build", and a sweep must not treat the second as the first.
* **Taken means observed taken.** ``items_taken`` counts only selections whose
  batch the engine verified landing whole; a partial ``CONTAINER_FULL`` stop
  records what stopped and counts nothing, because the adapter's own verify
  refused to bless the partial state.
* **Skips are first-class.** Memory answering "unchanged since the last
  inspection" (:meth:`~pz_agent_core.memory.SaveMemory.container_unchanged`
  against a revision computed from the *current* observation), a door refused
  locked or barricaded — named by reference — an unroutable candidate, a
  malformed square: each is a recorded reason in the final report, never a
  silent drop.
* **Bounded everything.** Candidates per mission, consecutive failures,
  batches per container, completion probes — each has a constant here, and
  every sub-action is additionally bounded by its own adapter budgets and by
  the goal's wall clock above all of it.

The unchanged-check leans on one documented assumption inherited from the
wave-1 memory seam: the mod's inventory tier enumerates the contents of every
container it describes, so a described container whose enumeration is empty
reads as *empty*, which can only ever match a remembered "opened and found
empty" digest — an unremembered or changed shelf always sends the mission to
look.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.common import world_square
from pz_agent_core.actions.adapters.movement import (
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
    MOVE_RETRY_POLICY,
)
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals.model import LootScope
from pz_agent_core.loot import LeaveReason, LootCategory, LootPolicy, Selection, select, summarise
from pz_agent_core.memory import MemoryValueError, container_tail, content_revision_of
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
    ContainerRef,
    ItemView,
    JsonDict,
    NearbyObject,
    Observation,
    ReasonCode,
    RefError,
)

__all__ = [
    "DEFAULT_LOOT_RADIUS",
    "ENDED_CANCELLED",
    "ENDED_COMPLETE",
    "ENDED_ENCUMBERED",
    "ENDED_IN_PROGRESS",
    "ENDED_NO_PROGRESS",
    "ENDED_UNPINNED",
    "MAX_BATCHES_PER_CONTAINER",
    "MAX_CANDIDATES_PER_MISSION",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_DISCOVERIES_PER_OBSERVATION",
    "SKIP_UNCHANGED",
    "ContainerUnchanged",
    "IsReserved",
    "LootMission",
    "LootMissionLimits",
    "MissionComplete",
    "MissionProbe",
    "MissionRefused",
    "MissionStep",
    "NextMissionMove",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: World containers one mission will ever put on its worklist (inspected plus
#: skipped). "Loot the area" over a warehouse district has to end; a sweep
#: that wants more than this is several missions, stated as several goals.
MAX_CANDIDATES_PER_MISSION: Final = 24

#: Consecutive sub-action failures (opens, inspects, transfers, stuck
#: journeys — anything that is not a typed world refusal like a locked door)
#: before the mission ends with ``NO_PROGRESS`` rather than grinding on.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: ``inventory.transfer_batch`` calls one container may consume. One selection
#: is one batch of at most eight items; a container worth more batches than
#: this is re-visitable by the next mission, whose memory check will see the
#: revision changed.
MAX_BATCHES_PER_CONTAINER: Final = 4

#: Completion probes a mission that ran no action may spend before it stops
#: claiming completion it cannot get observed. Each probe is one goal-seam
#: request, so the goal's own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: Nearby objects scanned for candidates per observation, mirroring the
#: memory seam's own sighting bound: the mod bounds the tier, and this seam
#: stays bounded by its own number rather than by trust in the sender's.
MAX_DISCOVERIES_PER_OBSERVATION: Final = 64

#: The Chebyshev sweep a ``scope=radius`` goal gets when it named none.
#: Ten squares is a room-and-a-bit around where the user was standing when
#: they asked — a deliberate near-miss of the 30-square ceiling, which is the
#: *maximum* wander the channel admits, not the default one.
DEFAULT_LOOT_RADIUS: Final = 10

#: How far away a candidate must be before the mission routes a
#: :class:`~pz_agent_core.navigation.Journey` at it. Inside this distance
#: ``container.open_nearby`` is itself the walk (it rides the same movement
#: capability), so a journey would be a second navigator for the same stretch.
_APPROACH_DISTANCE: Final = MAX_MOVE_DISTANCE_SQUARES

#: The arrival radius an approach journey aims for: the widest the movement
#: adapter accepts, because the journey only needs to bring the container
#: within ``open_nearby``'s own reach, not to stand on it — the container's
#: square is frequently the one square the character cannot occupy.
_APPROACH_RADIUS: Final = MAX_ARRIVAL_RADIUS


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

ENDED_IN_PROGRESS: Final = "in_progress"
ENDED_COMPLETE: Final = "complete"
ENDED_ENCUMBERED: Final = "encumbered"
ENDED_NO_PROGRESS: Final = "no_progress"
ENDED_UNPINNED: Final = "unpinned"
#: Written by the wrapper when it prunes a mission whose goal died under it —
#: a cancel, a disarm, an expiry. The mission itself never ends this way.
ENDED_CANCELLED: Final = "cancelled"

#: The one skip reason another subsystem's answer produces verbatim, exported
#: so the tests and the wrapper name the same constant.
SKIP_UNCHANGED: Final = "unchanged since last inspection"

_SKIP_NO_SQUARE: Final = "the reference names no world square"
_SKIP_UNDESCRIBED: Final = "the observation no longer describes the container"

#: What each callable port answers. Both must be deterministic for the
#: mission to be: the reserve list is the user's own words via
#: :class:`~pz_agent_cli.memory.SidecarMemory`, and the unchanged check is
#: :meth:`~pz_agent_core.memory.SaveMemory.container_unchanged` — or an
#: honest constant ``False`` when no memory is wired, which sends the mission
#: to look rather than sparing it a trip nothing vouches for.
IsReserved = Callable[[str], bool]
ContainerUnchanged = Callable[[str, str], bool]


# --------------------------------------------------------------------------
# the four frozen answers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissionStep:
    """One intermediate action: submit it to the loop's action channel."""

    request: ActionRequest


@dataclass(frozen=True, slots=True)
class MissionProbe:
    """A completion probe: return it out the goal seam.

    Emitted only by a mission that finished with no action ever running, so
    no engine result exists to succeed the goal with. The probe is a bounded
    move inside the character's own square; the loop settles the goal on the
    probe's real, observed result.
    """

    request: ActionRequest


@dataclass(frozen=True, slots=True)
class MissionComplete:
    """The criterion held and at least one real result proves work happened.

    The wrapper ends the goal through ``GoalQueue.succeed`` with the last
    succeeded channel result — the queue's one evidence-carrying route.
    """


@dataclass(frozen=True, slots=True)
class MissionRefused:
    """The mission is over short of its criterion, with a typed reason."""

    reason_code: ReasonCode
    detail: str


NextMissionMove = MissionStep | MissionProbe | MissionComplete | MissionRefused | None


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LootMissionLimits:
    """Every bound one mission runs under. All of them are load-bearing."""

    max_candidates: int = MAX_CANDIDATES_PER_MISSION
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_batches_per_container: int = MAX_BATCHES_PER_CONTAINER
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        for name in (
            "max_candidates",
            "max_consecutive_failures",
            "max_batches_per_container",
            "max_completion_probes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScopePin:
    """What "the area" resolved to, from the activation observation."""

    scope: LootScope
    room: str = ""
    building: str = ""
    centre: GridSquare | None = None
    radius: int = 0


class _Candidate:
    """One world container the mission owes a visit or a skip reason."""

    __slots__ = ("index", "ref", "square", "tail")

    def __init__(self, ref: str, tail: str, square: GridSquare, index: int) -> None:
        self.ref = ref
        self.tail = tail
        self.square = square
        self.index = index


#: The per-candidate pipeline stages, in the order they run.
_STAGE_START: Final = "start"
_STAGE_APPROACH: Final = "approach"
_STAGE_OPEN: Final = "open"
_STAGE_INSPECT: Final = "inspect"
_STAGE_TRANSFER: Final = "transfer"


def _chebyshev(a: GridSquare, b: GridSquare) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _current_revision(observation: Observation, ref: str) -> str | None:
    """The content revision of *ref* as this observation enumerates it, or None.

    None when the observation does not describe the container at all — the
    unchanged-check must then send the mission to look rather than infer.
    """
    inventory = observation.inventory
    if inventory is None or inventory.container(ref) is None:
        return None
    held = inventory.items_in(ref)
    return content_revision_of(Counter(item.full_type for item in held).items())


def _main_ref(observation: Observation) -> str:
    return str(ContainerRef.player_main(observation.session_id))


def _door_named(reason: ReasonCode | None, door_ref: str | None) -> str | None:
    """A door-shaped skip reason, or None when the refusal is not one."""
    if reason is ReasonCode.DOOR_LOCKED:
        return f"door {door_ref or 'unknown'} is locked"
    if reason is ReasonCode.DOOR_BARRICADED:
        return f"door {door_ref or 'unknown'} is barricaded"
    return None


class LootMission:
    """One ``loot_area`` goal, driven container by container.

    Created by the wrapper when the goal activates and discarded when the
    goal reaches a terminal state — the mission never outlives its goal. All
    inputs are injected and deterministic: the shared
    :class:`~pz_agent_core.navigation.LocalMap`, the frozen
    :class:`~pz_agent_core.loot.LootPolicy`, and the two memory callables.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        local_map: LocalMap,
        scope: LootScope,
        policy: LootPolicy,
        is_reserved: IsReserved,
        container_unchanged: ContainerUnchanged,
        radius: int = DEFAULT_LOOT_RADIUS,
        limits: LootMissionLimits | None = None,
        journey_limits: JourneyLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._key_stem = goal_id[:MAX_JOURNEY_ID_LEN]
        self._map = local_map
        self._scope = scope
        self._radius = radius
        self._policy = policy
        # Stored as attributes typed by use; the wrapper builds them from
        # whatever memory the assembled loop actually holds.
        self._is_reserved = is_reserved
        self._container_unchanged = container_unchanged
        self._limits = limits if limits is not None else LootMissionLimits()
        self._journey_limits = journey_limits if journey_limits is not None else JourneyLimits()

        self._pin: _ScopePin | None = None
        self._worklist: deque[_Candidate] = deque()
        self._seen: set[str] = set()
        self._candidates_recorded = 0
        self._truncated = False

        self._current: _Candidate | None = None
        self._stage = _STAGE_START
        self._journey: Journey | None = None
        self._pending_action: str | None = None
        self._last_selection: Selection | None = None
        self._batches = 0
        self._candidate_index = 0

        self._steps = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False
        self._encumbered_floor: float | None = None

        self._inspected: list[str] = []
        self._skipped: list[tuple[str, str]] = []
        self._taken: Counter[LootCategory] = Counter()
        self._left: Counter[LeaveReason] = Counter()
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

        Room and building names came out of the game, so they pass through
        :func:`~pz_agent_core.observation.compact.redact_text` like every
        other game-authored string this project renders. Category and reason
        keys render in their enums' declared order so two reports of the same
        mission serialise identically.
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
            "containers_inspected": list(self._inspected),
            "containers_skipped": [{"ref": ref, "reason": reason} for ref, reason in self._skipped],
            "items_taken": {
                category.value: self._taken[category]
                for category in LootCategory
                if self._taken[category]
            },
            "items_left": {
                reason.value: self._left[reason] for reason in LeaveReason if self._left[reason]
            },
            "ended": self._ended,
            "candidates_truncated": self._truncated,
        }

    def summary_line(self) -> str:
        """One bounded line of the report, for a goal record's ``detail``.

        Constants and counts only — no refs, no game-authored names — so it
        satisfies the goal channel's rule that a detail is never caller text.
        """
        return (
            f"loot {self._ended}: inspected={len(self._inspected)} "
            f"skipped={len(self._skipped)} taken={sum(self._taken.values())} "
            f"left={sum(self._left.values())}"
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
        if self._stage == _STAGE_APPROACH:
            journey = self._journey
            if journey is not None:
                journey.note_result(result)
            if result.status is ActionStatus.SUCCEEDED:
                self._note_success()
            return
        if result.status is ActionStatus.SUCCEEDED:
            self._note_success()
            if self._stage == _STAGE_OPEN:
                self._stage = _STAGE_INSPECT
            elif self._stage == _STAGE_INSPECT:
                self._stage = _STAGE_TRANSFER
            elif self._stage == _STAGE_TRANSFER:
                self._note_batch_landed()
            return
        self._note_failure(result)

    def _note_success(self) -> None:
        self._any_success = True
        self._consecutive_failures = 0

    def _note_batch_landed(self) -> None:
        """A whole batch was observed in the destination; count it as taken."""
        selection = self._last_selection
        self._last_selection = None
        self._batches += 1
        if selection is None:
            return
        counted = summarise(selection)
        for category, count in counted.taken_by_category.items():
            self._taken[category] += count
        more_wanted = any(entry.reason is LeaveReason.BATCH_FULL for entry in selection.leave)
        if more_wanted and self._batches < self._limits.max_batches_per_container:
            # The container holds more than one batch carries: stay on it and
            # re-select against the remaining contents on the next tick.
            return
        self._record_leaves(selection)
        self._finish_candidate()

    def _note_failure(self, result: ActionResult) -> None:
        current = self._current
        reason = result.reason_code
        door = _door_named(reason, self._door_ref_of(result))
        if self._stage in (_STAGE_OPEN, _STAGE_INSPECT):
            if door is not None:
                # A sealed door is a typed fact about the world, not the
                # mission failing to make progress; it costs no failure.
                self._skip_current(door)
                return
            self._count_failure()
            if current is not None:
                self._skip_current(f"{result.action} failed: {self._reason_token(reason)}")
            return
        if self._stage == _STAGE_TRANSFER:
            selection = self._last_selection
            self._last_selection = None
            if reason is ReasonCode.CONTAINER_FULL and selection is not None:
                # The honest partial stop: what the selection wanted is
                # recorded as left, what stopped is recorded on the report,
                # and the smallest wanted weight becomes the floor the next
                # observation's free capacity is held against.
                self._left[LeaveReason.OVER_CAPACITY] += len(selection.take)
                self._record_leaves(selection)
                if current is not None:
                    self._inspected.append(current.ref)
                self._encumbered_floor = min((pick.weight for pick in selection.take), default=None)
                self._reset_candidate()
                return
            self._count_failure()
            if selection is not None:
                self._record_leaves(selection)
            if current is not None:
                self._skip_current(f"{result.action} failed: {self._reason_token(reason)}")
            return

    def _reason_token(self, reason: ReasonCode | None) -> str:
        return reason.value if reason is not None else "UNKNOWN"

    @staticmethod
    def _door_ref_of(result: ActionResult) -> str | None:
        door_ref = result.evidence.get("door_ref")
        return door_ref if isinstance(door_ref, str) else None

    def _count_failure(self) -> None:
        self._consecutive_failures += 1

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: learn the map, pin the scope, discover candidates,
        check the encumbered floor, then drive the current candidate — or
        finish. A finished mission replays its terminal value.
        """
        if self._final is not None:
            return self._final
        self._map.observe(observation)
        if self._pin is None:
            refused = self._pin_scope(observation)
            if refused is not None:
                return refused
        self._discover(observation)
        if self._pending_action is not None:
            # A channel leg is still being driven; the map learned, the
            # mission decides on the observation after its result lands.
            return None
        encumbered = self._check_encumbered(observation)
        if encumbered is not None:
            return encumbered
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive sub-actions failed",
            )
        # Bounded by the worklist, which is bounded by max_candidates: each
        # iteration either emits a request, records a skip and drops the
        # candidate, or advances a stage that observed its own completion.
        while True:
            if self._current is None:
                self._start_next_candidate(observation)
                if self._current is None:
                    return self._finish(observation)
            move = self._drive_current(observation)
            if move is not None:
                return move

    # -- scope --------------------------------------------------------------

    def _pin_scope(self, observation: Observation) -> MissionRefused | None:
        player = observation.player
        if self._scope is LootScope.ROOM:
            if player.room is None:
                return self._refuse(
                    ENDED_UNPINNED,
                    ReasonCode.PRECONDITION_FAILED,
                    "the build reports no room for the character here; use scope=radius",
                )
            self._pin = _ScopePin(
                scope=self._scope, room=player.room, building=player.building or ""
            )
            return None
        if self._scope is LootScope.BUILDING:
            if player.building is None:
                return self._refuse(
                    ENDED_UNPINNED,
                    ReasonCode.PRECONDITION_FAILED,
                    "the build reports no building for the character here; use scope=radius",
                )
            self._pin = _ScopePin(scope=self._scope, building=player.building)
            return None
        self._pin = _ScopePin(
            scope=self._scope,
            centre=square_of(player.position),
            radius=self._radius,
        )
        return None

    def _in_scope(self, seen: NearbyObject, square: GridSquare) -> bool:
        pin = self._pin
        assert pin is not None  # _discover runs after _pin_scope
        if pin.scope is LootScope.ROOM:
            if seen.room is None or seen.room != pin.room:
                # None is "unreadable or outdoors", and neither is provably
                # this room; the container is out of scope, not skipped.
                return False
            return not pin.building or seen.building is None or seen.building == pin.building
        if pin.scope is LootScope.BUILDING:
            return seen.building is not None and seen.building == pin.building
        assert pin.centre is not None
        # Planar Chebyshev on purpose: floors are the journey's business, and
        # an unroutable floor becomes a recorded skip rather than a blind spot.
        return _chebyshev(pin.centre, square) <= pin.radius

    # -- candidates ---------------------------------------------------------

    def _discover(self, observation: Observation) -> None:
        nearby = observation.nearby
        if nearby is None:
            return
        for seen in nearby.objects[:MAX_DISCOVERIES_PER_OBSERVATION]:
            if seen.ref in self._seen:
                continue
            try:
                parsed = ContainerRef.parse(seen.ref)
            except RefError:
                # Doors, windows, corpses: ordinary neighbours, not errors.
                continue
            if not parsed.is_world:
                continue
            square = world_square(seen.ref)
            if square is None:
                # A world container whose tail encodes no square: recorded
                # honestly, because "reachable" cannot even be asked of it.
                self._record_candidate(seen.ref, skip=_SKIP_NO_SQUARE)
                continue
            if not self._in_scope(seen, square):
                continue
            try:
                tail = container_tail(seen.ref)
            except MemoryValueError:
                self._record_candidate(seen.ref, skip=_SKIP_NO_SQUARE)
                continue
            if self._unchanged(observation, seen.ref, tail):
                self._record_candidate(seen.ref, skip=SKIP_UNCHANGED)
                continue
            self._record_candidate(seen.ref, tail=tail, square=square)

    def _unchanged(self, observation: Observation, ref: str, tail: str) -> bool:
        revision = _current_revision(observation, ref)
        if revision is None:
            return False
        return bool(self._container_unchanged(tail, revision))

    def _record_candidate(
        self,
        ref: str,
        *,
        tail: str = "",
        square: GridSquare | None = None,
        skip: str | None = None,
    ) -> None:
        if self._candidates_recorded >= self._limits.max_candidates:
            # Deliberately not added to _seen: a later observation may find
            # room again only if earlier candidates were... they cannot be —
            # the ledger only grows — so the flag is the honest report of it.
            self._truncated = True
            return
        self._seen.add(ref)
        self._candidates_recorded += 1
        if skip is not None:
            self._skipped.append((ref, skip))
            return
        assert square is not None
        self._candidate_index += 1
        self._worklist.append(_Candidate(ref, tail, square, self._candidate_index))

    def _start_next_candidate(self, observation: Observation) -> None:
        while self._worklist:
            candidate = self._worklist.popleft()
            # Re-checked at start: the shelf may have been inspected by this
            # very mission's earlier walk past it, and memory now knows.
            if self._unchanged(observation, candidate.ref, candidate.tail):
                self._skipped.append((candidate.ref, SKIP_UNCHANGED))
                continue
            self._current = candidate
            self._stage = _STAGE_START
            self._journey = None
            self._batches = 0
            self._last_selection = None
            return

    # -- the per-candidate pipeline ------------------------------------------

    def _drive_current(self, observation: Observation) -> NextMissionMove:
        candidate = self._current
        assert candidate is not None
        if self._stage == _STAGE_START:
            here = square_of(observation.player.position)
            if (
                _chebyshev(here, candidate.square) > _APPROACH_DISTANCE
                or here[2] != candidate.square[2]
            ):
                self._journey = Journey(
                    self._map,
                    NavigationTarget(
                        x=candidate.square[0],
                        y=candidate.square[1],
                        z=candidate.square[2],
                        radius=_APPROACH_RADIUS,
                    ),
                    journey_id=f"{self._key_stem[: MAX_JOURNEY_ID_LEN - 8]}.c{candidate.index}",
                    limits=self._journey_limits,
                )
                self._stage = _STAGE_APPROACH
            else:
                self._stage = _STAGE_OPEN
        if self._stage == _STAGE_APPROACH:
            return self._drive_journey(observation)
        if self._stage == _STAGE_OPEN:
            return self._emit(
                observation,
                ActionName.CONTAINER_OPEN_NEARBY,
                {"container_ref": candidate.ref},
            )
        if self._stage == _STAGE_INSPECT:
            return self._emit(
                observation,
                ActionName.CONTAINER_INSPECT,
                {"container_ref": candidate.ref},
            )
        return self._drive_transfer(observation, candidate)

    def _drive_journey(self, observation: Observation) -> NextMissionMove:
        journey = self._journey
        assert journey is not None
        value = journey.next_step(observation)
        if isinstance(value, Arrived):
            self._journey = None
            self._stage = _STAGE_OPEN
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
            self._skip_current(door)
            return
        if error.reason_code is ReasonCode.PATH_NOT_FOUND:
            # A proven wall is a fact about the world like a locked door;
            # it costs a skip, not a failure.
            self._skip_current(f"approach refused: {error.failure.value}")
            return
        self._count_failure()
        self._skip_current(f"approach refused: {error.failure.value}")

    def _drive_transfer(self, observation: Observation, candidate: _Candidate) -> NextMissionMove:
        inventory = observation.inventory
        described = None if inventory is None else inventory.container(candidate.ref)
        if inventory is None or described is None:
            self._count_failure()
            self._skip_current(_SKIP_UNDESCRIBED)
            return None
        if self._batches >= self._limits.max_batches_per_container:
            self._inspected.append(candidate.ref)
            self._reset_candidate()
            return None
        contents: list[ItemView] = inventory.items_in(candidate.ref)
        main = inventory.container(_main_ref(observation))
        free = None if main is None else main.free_capacity
        selection = select(
            contents,
            policy=self._policy,
            free_capacity=free,
            is_reserved=self._is_reserved,
        )
        if not selection.take:
            self._record_leaves(selection)
            self._inspected.append(candidate.ref)
            self._reset_candidate()
            return None
        self._last_selection = selection
        return self._emit(
            observation,
            ActionName.INVENTORY_TRANSFER_BATCH,
            {
                "item_refs": list(selection.take_refs),
                "destination_container_ref": _main_ref(observation),
            },
        )

    def _emit(self, observation: Observation, action: ActionName, args: JsonDict) -> MissionStep:
        self._steps += 1
        request = ActionRequest(
            action=action,
            session_id=observation.session_id,
            idempotency_key=f"loot:{self._key_stem}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)

    # -- endings -------------------------------------------------------------

    def _check_encumbered(self, observation: Observation) -> MissionRefused | None:
        floor = self._encumbered_floor
        if floor is None:
            return None
        inventory = observation.inventory
        main = None if inventory is None else inventory.container(_main_ref(observation))
        free = None if main is None else main.free_capacity
        if free is not None and free < floor:
            return self._refuse(
                ENDED_ENCUMBERED,
                ReasonCode.CONTAINER_FULL,
                "the main inventory cannot fit the smallest wanted item",
            )
        # Capacity unknown, or room reappeared (something was dropped or
        # eaten): the proof of fullness is gone, so the sweep continues.
        self._encumbered_floor = None
        return None

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
        # Complete without a single action run: nothing exists to succeed the
        # goal with, and minting a result would fabricate evidence. The probe
        # is the engine observing an ordinary fact instead — see the module
        # docstring. Numbered so a failed probe's retry never reuses a key.
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"loot:{self._key_stem}:done{self._probes}",
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

    # -- candidate bookkeeping ----------------------------------------------

    def _skip_current(self, reason: str) -> None:
        current = self._current
        if current is not None:
            self._skipped.append((current.ref, reason))
        self._reset_candidate()

    def _finish_candidate(self) -> None:
        current = self._current
        if current is not None:
            self._inspected.append(current.ref)
        self._reset_candidate()

    def _reset_candidate(self) -> None:
        self._current = None
        self._journey = None
        self._stage = _STAGE_START
        self._batches = 0
        self._last_selection = None

    def _record_leaves(self, selection: Selection) -> None:
        for entry in selection.leave:
            self._left[entry.reason] += 1
