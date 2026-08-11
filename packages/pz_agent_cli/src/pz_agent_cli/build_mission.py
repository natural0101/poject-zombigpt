"""``build_structure``: one structure, one square, one command, once.

The craft mission's sibling one rung stricter, and the rung is the whole
difference between them. A craft spends two planks: the planks are gone and no
later observation puts them back, but the world is where it was. A build puts a
permanent object *on a square*, and this project ships no action that takes one
back down — removing what somebody else put there is a different authority and
this build does not have it. Everything below follows from that single
asymmetry, and each clause is pinned by a test:

* **The policy decides, before the one command.**
  :func:`~pz_agent_core.policy.building.assess_build` is asked against the
  newest observation, and it is asked *before* anything is queued, so every
  refusal costs a sentence rather than a wall. The mission itself never reads a
  square, never counts a plank and never decides that a placement is safe.
* **One structure, one square, no loop.** There is no ``count`` — not in the
  goal's parameters, not in the command's arguments, not here. The attempt
  budget is :data:`MAX_BUILD_ATTEMPTS`, which is one, and a limits object cannot
  raise it: a second structure is a second submission, through the policy, the
  P4 gate and the safety stop again.
* **A refused placement is not retried somewhere else.** The square is the
  user's choice. When the policy refuses it the mission says so with the typed
  reason and stops; picking a different square would be a decision nobody
  delegated, and the one square this side could pick unaided is the square the
  character is standing on.
* **The mission never goes gathering.** Short of materials it ends the goal with
  ``RECIPE_MATERIALS_MISSING`` and the shortfall named. The craft mission set
  that precedent for the same reason: chaining a build onto a loot is a decision
  about the user's time and the character's safety, and making it here would be
  an errand nobody ordered.
* **Success is the structure, observed standing.** The mission completes only
  when the newest observation reads the square back as something's — an object
  standing there that the observation the mission started from did not carry, or
  the observer's own assessment of the square turning to blocked when it was not
  blocked before. A build the mod called finished is a statement about the
  queue; :data:`MAX_CONFIRMATION_LOOKS` bounds how many observations the mission
  will wait for the real answer before reporting honestly that it never saw one.

What the trapping check can and cannot prove, restated here because this is
where it reaches a report a user reads. :func:`~pz_agent_core.policy.building.
enclosure_after` searches the squares *this observation described*. It cannot
show the character is not already enclosed by something beyond that window —
that would need a map this side does not have. It shows that this placement does
not remove the last exit in view, and
:attr:`~pz_agent_core.policy.building.EnclosureCheck.claim` says so in the words
the report carries verbatim. Erring toward refusal is correct here and the
module is written that way throughout: a refused wall costs a sentence, a wall
that traps the character costs the save.

Two renderings of game-authored text live here, the craft mission's pair for the
craft mission's reasons: the report carries names through
:func:`~pz_agent_core.observation.compact.redact_text` because a reader wants to
know what the thing is called, and the goal record's one-line ``detail`` carries
them through :func:`~pz_agent_cli.craft_mission._detail_token` instead, because
that line's *construction* fails if it runs long or holds a line break. A goal
that could not be failed because its failure text was too long is the failure
mode both filters exist to make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.navigation.local_map import GridSquare, square_of
from pz_agent_core.observation.compact import redact_text
from pz_agent_core.policy.building import (
    BuildingDecision,
    BuildingRefusal,
    EnclosureCheck,
    ObservedWindow,
    Square,
    assess_build,
    read_window,
)
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.crafting import Shortfall
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    JsonDict,
    Observation,
    ReasonCode,
    SquareRef,
)

# The craft mission's three renderers, imported rather than copied. A build's
# shortfall list and a craft's are the same list rendered under the same
# one-line bound for the same goal record, and a second implementation here
# would be a second thing to keep in step — with the half that drifted being the
# half whose refusal a goal record refuses to hold. They are private to that
# module only because it had no sibling until this wave.
from .craft_mission import _detail_token, _render_count, _shortfall_line
from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    PHASE_START,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "BUILD_PHASES",
    "ENDED_REFUSED",
    "ENDED_UNCONFIRMED",
    "MAX_BUILD_ATTEMPTS",
    "MAX_COMPLETION_PROBES",
    "MAX_CONFIRMATION_LOOKS",
    "PHASE_BUILD",
    "PHASE_CONFIRM",
    "REFUSAL_REASONS",
    "BuildMissionLimits",
    "BuildStructureMission",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Build commands one mission may issue, and the ceiling no limits object may
#: raise. It is one, and one is not a placeholder for a larger number. A craft
#: that failed may be re-run because the materials it would spend are still
#: countable in the bag; a build that failed may or may not have put something
#: on the square, and a second command is a second irreversible attempt made on
#: this side's initiative rather than the user's. When the placement did land,
#: the next observation reads the square as occupied and the mission completes
#: on that; when it did not, the honest answer is a report, and the user's next
#: sentence is a new submission through the P4 gate.
MAX_BUILD_ATTEMPTS: Final = 1

#: Observations the mission will wait through, after its one command, for the
#: square to read back with something standing on it. Three, because the mod
#: answers a build on its own thirty-second window and the sidecar observes
#: several times inside one: fewer would report "never observed" for a wall that
#: was simply mid-placement, and more would be a mission waiting out its goal's
#: wall clock on a command that already ended. Exhausting them is a failure with
#: its own token, never a success on the strength of the command's own ack.
MAX_CONFIRMATION_LOOKS: Final = 3

#: Completion probes a mission that ran no successful command may spend — the
#: loot and craft missions' arrangement: each probe is one goal-seam request, so
#: the goal's own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

#: The building policy refused: the structure is unknown, the square could not
#: be read, something already stands there, the placement would seal the
#: character in, or the materials are short or reserved. Nothing was queued —
#: the policy is asked before a command exists to send.
ENDED_REFUSED: Final = "refused"

#: The one command went out and the structure was never observed standing on
#: the square within :data:`MAX_CONFIRMATION_LOOKS` observations. Deliberately
#: its own token rather than ``no_progress``: "I asked for a wall and never saw
#: one" is a different thing to tell a user from "I could not act at all", and
#: it is the token a reader checks when the world may or may not have changed.
ENDED_UNCONFIRMED: Final = "unconfirmed"

#: The pipeline phases. ``start`` is the loot mission's token, imported rather
#: than restated; ``build`` is the one command; ``confirm`` is the wait for the
#: square to read back, which is a phase precisely because it is the part that
#: proves the goal rather than the part that asks for it.
PHASE_BUILD: Final = "build"
PHASE_CONFIRM: Final = "confirm"

BUILD_PHASES: Final = (PHASE_START, PHASE_BUILD, PHASE_CONFIRM)

#: The building policy's refusal token -> the protocol reason the goal ends
#: with. A second copy of the table
#: :mod:`pz_agent_core.actions.adapters.building` keeps for its own validation
#: refusals, and knowingly so, exactly as the craft mission keeps a second copy
#: of the crafting adapter's: that one is private to the adapter and fires at
#: validation time, while this one fires before an adapter exists to ask. The
#: two must agree, which a test asserts by reading both. Total over the enum —
#: also pinned by a test.
REFUSAL_REASONS: Final[dict[BuildingRefusal, ReasonCode]] = {
    BuildingRefusal.STRUCTURE_UNKNOWN: ReasonCode.RECIPE_UNKNOWN,
    BuildingRefusal.SQUARE_UNREADABLE: ReasonCode.TARGET_NOT_LOADED,
    BuildingRefusal.SQUARE_OCCUPIED: ReasonCode.SQUARE_OCCUPIED,
    BuildingRefusal.WOULD_TRAP_PLAYER: ReasonCode.WOULD_TRAP_PLAYER,
    BuildingRefusal.MATERIALS_MISSING: ReasonCode.RECIPE_MATERIALS_MISSING,
    BuildingRefusal.MATERIALS_RESERVED: ReasonCode.RESOURCE_RESERVED,
}


# --------------------------------------------------------------------------
# reading the square
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SquareReading:
    """What one observation says is standing on the target square.

    Two facts and no interpretation: the references of the objects the observer
    put on the square, and whether the observer calls the square taken at all
    (an object on it, or its own ``blocked`` assessment). The postcondition is a
    *difference* between two of these, which is why the reading is a value the
    mission keeps rather than a predicate it evaluates once.
    """

    refs: frozenset[str]
    occupied: bool


def _cause_of(decision: BuildingDecision, refusal: BuildingRefusal) -> str:
    """One clause of a goal record's detail, per refusal token.

    Total over :class:`~pz_agent_core.policy.building.BuildingRefusal` and
    written as six branches rather than one interpolated sentence, because each
    of them says something different to the person who asked for the wall: two
    are "not this square", one is "I cannot see that square well enough to
    say", one is "not this structure", one is "not with what you are carrying"
    and one is "not with what you told me to keep". The policy's own ``detail``
    is richer and goes to the report; this is the line the goal record will
    hold, so it is assembled from constants and the mission's own bounded
    renderers.
    """
    listed = _shortfall_line(decision.shortfalls)
    if refusal is BuildingRefusal.WOULD_TRAP_PLAYER:
        # The one refusal whose reason outranks its subject: a wall this agent
        # raises is a wall this agent cannot take back down, so a placement
        # whose consequences could not be computed is refused like one whose
        # consequences were computed and were bad.
        return "that placement would leave no way out of the squares this observation showed"
    if refusal is BuildingRefusal.SQUARE_OCCUPIED:
        return "something already stands on that square, and clearing one is not mine to do"
    if refusal is BuildingRefusal.SQUARE_UNREADABLE:
        return "that square is not one this observation can describe"
    if refusal is BuildingRefusal.STRUCTURE_UNKNOWN:
        named = _detail_token(_decision_name(decision))
        return f"the structure {named} is not one I saw a way to build"
    if refusal is BuildingRefusal.MATERIALS_RESERVED:
        return f"short only because you reserved what it needs: {listed}"
    return f"short of {listed}"


def _decision_name(decision: BuildingDecision) -> str:
    """The structure a decision is about, or a stand-in when none was read.

    A refusal for a structure nothing observed carries no
    :class:`~pz_agent_core.policy.building.StructureView` at all, and a blank
    where a name belongs reads as a bug in the reporter rather than as the
    finding it is.
    """
    return "unnamed" if decision.structure is None else decision.structure.name


def _read_square(observation: Observation, square: Square) -> _SquareReading | None:
    """The reading, or None when this observation cannot describe the square.

    None is not "empty". A window the mod did not produce, a window this side
    refused to read whole, and a square the observer did not mention are all
    "nothing is known about that square right now", and a mission that treated
    any of them as "nothing is standing there" would either build onto ground it
    cannot see or claim a structure it never observed.
    """
    window: ObservedWindow | None = read_window(observation, square[2])
    if window is None or not window.describes(square):
        return None
    return _SquareReading(
        refs=frozenset(seen.ref for seen in window.standing_on(square)),
        occupied=window.is_occupied(square),
    )


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildMissionLimits:
    """Every bound one mission runs under.

    ``max_attempts`` may be narrowed and cannot be widened, and at
    :data:`MAX_BUILD_ATTEMPTS` = 1 that means it can only ever be one: how many
    irreversible commands a single submission authorises is a safety property,
    not a tuning. The field exists all the same, because a bound that is not
    stated anywhere is a bound nothing can assert about.
    """

    max_attempts: int = MAX_BUILD_ATTEMPTS
    max_confirmation_looks: int = MAX_CONFIRMATION_LOOKS
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_BUILD_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be within 1..{MAX_BUILD_ATTEMPTS}, got {self.max_attempts}"
            )
        for name in ("max_confirmation_looks", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# the mission
# --------------------------------------------------------------------------


class BuildStructureMission:
    """One ``build_structure`` goal: ask the policy, send one command, look.

    Created by the wrapper when the goal activates and discarded when the goal
    reaches a terminal state — the mission never outlives its goal. All inputs
    are injected and deterministic: the structure and the square the goal
    validated, the policy configuration the assembled loop holds (the user's own
    reserves are part of it), and the frozen limits. The mission never decides
    whether a placement is safe (:func:`assess_build` does), never counts a
    material (the policy does) and never proves a structure (the adapter does):
    what it owns is when to ask, once, and when to stop.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        structure: str,
        square: Square,
        policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
        limits: BuildMissionLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        if not structure:
            raise ValueError("a build mission must name the structure it raises")
        if len(square) != 3:
            raise ValueError("a build mission must name one square as (x, y, z)")
        self._goal_id = goal_id
        self._structure = structure
        self._square: Square = (int(square[0]), int(square[1]), int(square[2]))
        self._policy = policy
        self._limits = limits if limits is not None else BuildMissionLimits()

        self._stage = PHASE_START
        self._pending_action: str | None = None
        self._steps = 0
        self._probes = 0
        self._any_success = False

        #: What the square held when the mission first managed to read it. The
        #: postcondition is measured against this and nothing else, which is
        #: what makes "the structure is standing" mean *appeared* rather than
        #: "something is there" — a square that was taken all along would
        #: otherwise prove a wall the mission never raised.
        self._baseline: _SquareReading | None = None
        self._attempts = 0
        self._builds_succeeded = 0
        self._looks = 0
        self._placed = False
        self._refusal: str | None = None
        self._shortfalls: tuple[Shortfall, ...] = ()
        self._occupied_by: tuple[str, ...] = ()
        self._enclosure: EnclosureCheck | None = None
        self._needs_travel: bool | None = None
        self._ended = ENDED_IN_PROGRESS
        self._final: MissionComplete | MissionRefused | None = None

    # -- read-only state ----------------------------------------------------

    @property
    def goal_id(self) -> str:
        return self._goal_id

    @property
    def structure(self) -> str:
        """The blueprint this mission raises. Never interpreted here."""
        return self._structure

    @property
    def square(self) -> Square:
        """The square the user chose. The mission never chooses another."""
        return self._square

    @property
    def ended(self) -> str:
        return self._ended

    @property
    def placed(self) -> bool:
        """Whether the structure was *observed* standing on the square."""
        return self._placed

    @property
    def phase(self) -> str:
        """Where the mission stands: one of :data:`BUILD_PHASES`.

        ``start`` covers "not yet begun"; ``build`` is the one command in
        flight; ``confirm`` is the wait for the square to read back. A mission
        that already ended keeps the phase it stopped in, because whether it
        ended is :attr:`ended`'s answer, not this one's.
        """
        return self._stage

    @property
    def any_success(self) -> bool:
        """Whether any channel action this mission asked for succeeded."""
        return self._any_success

    def mark_abandoned(self) -> None:
        """Seal the report of a mission whose goal died under it.

        The wrapper's prune path. Only a mission still in flight is resealed — a
        mission that already ended keeps the ending it earned.
        """
        if self._ended is ENDED_IN_PROGRESS:
            self._ended = ENDED_CANCELLED

    @property
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        Counts, closed tokens, and game-authored names run through
        :func:`~pz_agent_core.observation.compact.redact_text`. Two entries earn
        their place beyond the ordinary bookkeeping. ``enclosure`` carries the
        trapping check's own answer *including its claim*, so a reader is told
        in the same breath what was proven and what could not be: the window is
        bounded, and a report that said "safe" without that sentence would be
        overstating the only thing this wave is about. ``occupied_by`` names
        what was found standing on the square, because "something is already
        there" is only actionable if a user learns what.
        """
        return {
            "structure": redact_text(self._structure),
            "square": {"x": self._square[0], "y": self._square[1], "z": self._square[2]},
            "attempts": self._attempts,
            "builds_succeeded": self._builds_succeeded,
            "confirmation_looks": self._looks,
            "placed": self._placed,
            "refusal": self._refusal,
            "shortfalls": [
                {**short.as_dict(), "full_type": redact_text(short.full_type)}
                for short in self._shortfalls
            ],
            "occupied_by": [redact_text(name) for name in self._occupied_by],
            "enclosure": None if self._enclosure is None else self._enclosure.as_dict(),
            "needs_travel": self._needs_travel,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line for a goal record's ``detail``.

        Constants, counts, the square's own coordinates and the policy's closed
        refusal token — no refs, no game-authored names — so it satisfies the
        goal channel's rule that a detail is never caller text. The coordinates
        go through the same width guard the material counts do: a channel-
        admitted goal cannot carry a coordinate outside its declared range, but
        a mission is constructible without one, and a detail line whose length a
        caller chose is a detail line the goal record would refuse.
        """
        refusal = self._refusal if self._refusal is not None else "none"
        return (
            f"build {self._ended}: structure at "
            f"({_render_count(self._square[0])}, {_render_count(self._square[1])}) "
            f"placed={'yes' if self._placed else 'no'} attempts={self._attempts} "
            f"refusal={refusal}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Non-terminal acks and results for actions this mission did not ask for
        are ignored — the mission has at most one step in flight, and only ever
        one in its whole life. A failed build is *not* retried and needs no
        retirement bookkeeping: the attempt budget is already spent, so the
        mission's next move is to look at the square and then report.
        """
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True
            self._builds_succeeded += 1
        self._stage = PHASE_CONFIRM

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed and each place in it is load-bearing: replay a sealed
        ending, wait out the one step in flight, read the square (an
        unreadable square before the command is the policy's refusal to make,
        not this mission's), take the observed postcondition before any budget,
        then spend the one attempt — and only after asking the policy again.
        Every branch either emits the one bounded command, ends the mission, or
        waits for a fresh observation, and the attempt and confirmation budgets
        bound how long the loop can be kept walking through here.
        """
        if self._final is not None:
            return self._final
        if self._pending_action is not None:
            return None

        reading = _read_square(observation, self._square)
        if reading is not None and self._baseline is None:
            self._baseline = reading

        if self._observed_standing(reading):
            self._placed = True
            return self._finish(observation)

        if self._attempts >= self._limits.max_attempts:
            # The command has been sent and the square does not read back with
            # anything new on it. Look again a bounded number of times, then
            # say so — a build the mod called finished is a statement about the
            # queue, and this mission will not turn one into a wall.
            self._looks += 1
            if self._looks < self._limits.max_confirmation_looks:
                return None
            return self._refuse(
                ENDED_UNCONFIRMED,
                ReasonCode.NO_PROGRESS,
                (
                    "the build command went out and the structure was never observed "
                    f"standing after {self._looks} looks"
                    if self._any_success
                    else "the build command failed and nothing was observed standing"
                ),
            )

        decision = assess_build(observation, self._structure, self._square, self._policy)
        self._needs_travel = decision.needs_travel
        self._enclosure = decision.enclosure
        if decision.refused:
            return self._refuse_policy(decision)

        self._attempts += 1
        self._stage = PHASE_BUILD
        # Two arguments and no third: the blueprint and the square, exactly as
        # the adapter parses them. There is no count to send because there is no
        # count anywhere on this rung — a number here is the first thing a loop
        # in the mod would read.
        return self._emit(
            observation,
            ActionName.BUILDING_BUILD,
            {
                "blueprint": self._structure,
                "square": str(
                    SquareRef(
                        session_id=observation.session_id,
                        x=self._square[0],
                        y=self._square[1],
                        z=self._square[2],
                    )
                ),
            },
        )

    # -- the postcondition ---------------------------------------------------

    def _observed_standing(self, reading: _SquareReading | None) -> bool:
        """Is something on the square now that was not on it when we started?

        The adapter's own ``verify`` question, asked again at the goal's scale
        and answered the same two ways: a reference on the square that the
        baseline reading did not carry, or the observer's assessment of the
        square turning to blocked when it was not blocked before. Both are
        *differences*, which is why an unreadable square (``None``) and a
        missing baseline both answer "no": there is no change to observe, and a
        square that always had something on it proves nothing about this
        mission's command.
        """
        baseline = self._baseline
        if reading is None or baseline is None:
            return False
        if reading.refs - baseline.refs:
            return True
        return reading.occupied and not baseline.occupied

    # -- endings -------------------------------------------------------------

    def _refuse_policy(self, decision: BuildingDecision) -> MissionRefused:
        """The building policy's refusal, as the goal's typed end.

        Nothing has been queued when this fires, which is the whole design: the
        policy is asked before a command exists, so a placement that would seal
        the character in costs a sentence and no world change. The mission does
        not then try the next square along — the square is the user's choice and
        choosing another is a decision nobody delegated — and it does not go
        looting for a missing plank, for the craft mission's reason.
        """
        refusal = decision.refusal
        assert refusal is not None  # a refused decision carries its token
        self._refusal = refusal.value
        self._shortfalls = decision.shortfalls
        self._occupied_by = decision.occupied_by
        return self._refuse(ENDED_REFUSED, REFUSAL_REASONS[refusal], _cause_of(decision, refusal))

    def _finish(self, observation: Observation) -> NextMissionMove:
        """Complete — through the last real result, or through one probe.

        The craft mission's arrangement, and the no-command case is real here
        too: the observed postcondition is a change on the square, so a player
        who raises the wall themselves while the goal waits — or a build whose
        result was lost while the structure landed — satisfies it with no result
        of this mission's to succeed the goal with. Minting one would fabricate
        evidence; the probe is a bounded move inside the character's own square,
        numbered so a failed probe's retry never reuses a key.
        """
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
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here: GridSquare = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"build:{self._goal_id}:done{self._probes}",
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

    # -- machinery -----------------------------------------------------------

    def _emit(self, observation: Observation, action: ActionName, args: JsonDict) -> MissionStep:
        self._steps += 1
        request = ActionRequest(
            action=action,
            session_id=observation.session_id,
            idempotency_key=f"build:{self._goal_id}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)
