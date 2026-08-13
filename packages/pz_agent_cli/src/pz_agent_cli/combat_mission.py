"""``engage_single_zombie``: the deterministic combat mission — the «убей» half.

The threat directive's other half («если одиночного зомби безопасно убить —
убей, иначе отступи») on the ASSISTED rung only: a human submitted the goal,
and everything after that submission is deterministic. The mission is a state
machine over observations, shaped like
:class:`~pz_agent_cli.avoid_mission.AvoidMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` drives it through the same seam with
the same frozen move vocabulary imported from
:mod:`pz_agent_cli.loot_mission`: every equip, shove, attack window and
retreat travels the loop's action channel as a
:class:`~pz_agent_cli.loot_mission.MissionStep` — no step's own success is the
goal's postcondition — and the goal ends through the queue, or through one
:class:`~pz_agent_cli.loot_mission.MissionProbe` when nothing ever needed to
run.

The safety shape, each clause pinned by a test:

* **The policy is re-run before EVERY window.** Each pass through
  :meth:`EngageZombieMission.next_step` re-reads the picture and asks
  :func:`~pz_agent_core.combat.assess_engagement` again, so deterioration
  between windows — endurance draining, panic rising, a wound opening, a
  second zombie entering the close ring, the weapon breaking — is caught at
  the next window boundary at the latest and answers with a retreat, never
  with another swing.
* **Retreat on deterioration is mandatory, and it is the ending.** A policy
  refusal at any window emits one ``combat.retreat`` and then ends the goal
  with a typed failure whose detail carries the refusal token and the honest
  counts. The one exception is the first ``weapon_unusable``, which earns one
  ``combat.equip_best`` — a draw, not a swing — before the policy is asked
  again; a second one retreats like everything else.
* **One target, ever.** The mission pins the nearest observed zombie at its
  first threatened observation and never re-aims: a second zombie is
  deterioration (the policy's group gate), not a second target, and a pinned
  target that leaves observation un-fought ends the mission honestly rather
  than promoting whatever wandered closer.
* **No "attack until dead" anywhere.** At most :data:`MAX_WINDOWS` windows,
  each its own bounded ``combat.engage`` command through the loop's channel —
  so ``safety.stop``, the reflex guard and the user interrupt *between*
  windows structurally, because between windows nothing is running.
* **Success is the re-observed zombie.** The mission completes only when the
  newest observation reports the pinned target down, or the engage adapter's
  own absence rule proved it gone; the swing is never trusted, and a mission
  that never ran an action (the target was already down at activation)
  completes through the probe, exactly the loot arrangement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.combat import (
    DOWN_STATES,
    CombatPolicy,
    CombatRefusal,
    EngagementVerdict,
    assess_engagement,
)
from pz_agent_core.combat.policy import DEFAULT_COMBAT_POLICY
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
    PHASE_START,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "COMBAT_PHASES",
    "ENDED_EXHAUSTED",
    "ENDED_LOST",
    "ENDED_NO_TARGET",
    "ENDED_RETREATED",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_WINDOWS",
    "CombatMissionLimits",
    "EngageZombieMission",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Attack windows one mission may spend, and the hard ceiling no limits
#: object may raise. Four bounded windows is a fight that is not working;
#: the honest fifth move is the retreat, not a fifth window.
MAX_WINDOWS: Final = 4

#: Consecutive failed channel actions before the mission ends. The other
#: missions' number, restated because missions may diverge and a shared
#: constant would hide the review when one does.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no action may spend — the loot
#: mission's arrangement: each probe is one goal-seam request, so the goal's
#: own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

#: The policy refused at some window; one retreat was emitted and the goal
#: ended typed, with the refusal token in the report.
ENDED_RETREATED: Final = "retreated"

#: The window budget ran out with the target still standing; one retreat was
#: emitted and the goal ended typed.
ENDED_EXHAUSTED: Final = "exhausted"

#: No zombie was observed at activation. A kill order with nothing observed
#: to kill is a refusal, never a vacuous success — "gone before I looked" and
#: "put down" must not share a claim.
ENDED_NO_TARGET: Final = "no_target"

#: The pinned target left observation without being put down by an observed
#: window. Honest and distinct from complete: out of view is not down.
ENDED_LOST: Final = "lost"

#: The per-drive pipeline phases. ``start`` is the loot mission's token,
#: imported rather than restated; the four below are minted here, closed
#: tokens like every phase this project publishes.
PHASE_EQUIP: Final = "equip"
PHASE_SHOVE: Final = "shove"
PHASE_ENGAGE: Final = "engage"
PHASE_RETREAT: Final = "retreat"

COMBAT_PHASES: Final = (PHASE_START, PHASE_EQUIP, PHASE_SHOVE, PHASE_ENGAGE, PHASE_RETREAT)


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CombatMissionLimits:
    """Every bound one mission runs under. ``max_windows`` may be narrowed,
    never widened: :data:`MAX_WINDOWS` is the ceiling a limits object cannot
    raise, because the window budget is a safety property, not a tuning."""

    max_windows: int = MAX_WINDOWS
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        if not 1 <= self.max_windows <= MAX_WINDOWS:
            raise ValueError(f"max_windows must be within 1..{MAX_WINDOWS}, got {self.max_windows}")
        for name in ("max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# the mission
# --------------------------------------------------------------------------


class EngageZombieMission:
    """One ``engage_single_zombie`` goal, driven window by bounded window.

    Created by the wrapper when the goal activates and discarded when the
    goal reaches a terminal state — the mission never outlives its goal. All
    inputs are injected and deterministic: the frozen policy and the frozen
    limits. The mission never selects a weapon (``combat.equip_best`` and the
    mod's own deterministic pick own that) and never selects a route (the
    retreat here is the one-step ``combat.retreat``; real retreats are the
    avoid mission's).
    """

    def __init__(
        self,
        goal_id: str,
        *,
        policy: CombatPolicy = DEFAULT_COMBAT_POLICY,
        limits: CombatMissionLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._policy = policy
        self._limits = limits if limits is not None else CombatMissionLimits()

        self._stage = PHASE_START
        self._pending_action: str | None = None
        self._steps = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False

        self._target_ref: str | None = None
        self._windows = 0
        #: Whether a ``combat.engage`` **window** was itself observed
        #: succeeding. Only that adapter's verify carries the absence rule
        #: ("down, or honestly gone"), so only that success may read a
        #: vanished target as a kill — a shove proves "down *or further
        #: away*" and an equip proves nothing about the zombie at all.
        self._window_succeeded = False
        self._shoves = 0
        self._equip_requested = False
        self._refusal: str | None = None
        #: Sealed once a retreat has been emitted: (ended token, reason,
        #: cause). The next call after the retreat's result folds in ends the
        #: mission with exactly this — a retreat is the last command a
        #: refused fight ever sends.
        self._end_after_retreat: tuple[str, ReasonCode, str] | None = None
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
        """Where the mission stands: one of :data:`COMBAT_PHASES`.

        ``start`` covers "not yet begun" and "between windows"; a mission
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

        The wrapper's prune path. Only a mission still in flight is resealed —
        a mission that already ended keeps the ending it earned.
        """
        if self._ended is ENDED_IN_PROGRESS:
            self._ended = ENDED_CANCELLED

    @property
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        Counts and closed tokens only — the refusal token is the policy's
        own enum value, never game text — plus whether a target was ever
        pinned. The deliverable the directive's confirmation reads back is
        ``windows_fought``/``shoves``/``ended``.
        """
        return {
            "target_pinned": self._target_ref is not None,
            "windows_fought": self._windows,
            "shoves": self._shoves,
            "equip_requested": self._equip_requested,
            "refusal": self._refusal,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line for a goal record's ``detail``.

        Constants, counts and the policy's closed refusal token only — no
        refs, no game-authored names — so it satisfies the goal channel's
        rule that a detail is never caller text.
        """
        refusal = self._refusal if self._refusal is not None else "none"
        return (
            f"combat {self._ended}: windows={self._windows} shoves={self._shoves} refusal={refusal}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Non-terminal acks and results for actions this mission did not ask
        for are ignored — the mission has at most one step in flight. A
        retreat's failure is deliberately *not* counted into the failure
        streak: the mission is already ending, and the streak exists to stop
        work, not to re-judge the stopping.
        """
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True
            self._consecutive_failures = 0
            if self._stage == PHASE_ENGAGE:
                self._window_succeeded = True
        elif self._end_after_retreat is None:
            self._consecutive_failures += 1
        self._stage = PHASE_START if self._end_after_retreat is None else PHASE_RETREAT

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: replay a sealed ending, wait out the one step in
        flight, seal a post-retreat ending, stop a failing mission, read the
        observed postcondition (the target down beats every budget), then
        run the policy and act on its verdict. Every branch either emits a
        bounded request, ends the mission, or waits for a fresh observation;
        the window and failure budgets bound how long the loop can be kept
        walking through here.
        """
        if self._final is not None:
            return self._final
        if self._pending_action is not None:
            return None
        if self._end_after_retreat is not None:
            # The retreat this ending owed has been driven to its own terminal
            # result, so nothing of ours is in flight. The refused-admission
            # history this used to name as well cannot reach here: _emit sets
            # _pending_action before the planner ever offers the step, only a
            # matching terminal result clears it, and the guard above returns
            # on it. Since 5757008 that case does not survive to be asked
            # anyway -- the wrapper ends the goal typed instead of dropping
            # the step. Seal.
            ended, reason, cause = self._end_after_retreat
            return self._refuse(ended, reason, cause)
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            # A retreat here would be a fourth command into whatever is
            # refusing the first three (the channel, the capability, the
            # mod), so this ending — alone — does not retreat first.
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive combat actions failed",
            )
        if observation.nearby is None:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod sent no nearby tier this tick, so no target can be observed",
            )
        zombies = tuple(observation.nearby.zombies)

        if self._target_ref is None:
            target = _nearest(zombies)
            if target is None:
                # A kill order with nothing observed to kill: refused, never
                # vacuously succeeded — see ENDED_NO_TARGET.
                return self._refuse(
                    ENDED_NO_TARGET,
                    ReasonCode.PRECONDITION_FAILED,
                    "no zombie is observed to engage",
                )
            self._target_ref = target.ref
        target_ref = self._target_ref
        observed = next((zombie for zombie in zombies if zombie.ref == target_ref), None)

        # The observed postcondition, before any budget: the pinned target
        # down in this very observation ends the mission on real evidence.
        if observed is not None and observed.state is not None and observed.state in DOWN_STATES:
            return self._finish(observation)
        if observed is None:
            if self._window_succeeded:
                # The engage adapter's absence rule already decided the hard
                # half sidecar-side: a window only *succeeds* when the target
                # was re-observed down or honestly gone, so a vanished target
                # after a succeeded window is that verified outcome. It must
                # be that adapter's own success and no other: a shove verifies
                # "down or further away", which is the likeliest reason a
                # target leaves observation without being killed.
                return self._finish(observation)
            return self._refuse(
                ENDED_LOST,
                ReasonCode.PRECONDITION_FAILED,
                "the target left observation before being engaged; out of view is not down",
            )

        # The policy, EVERY window — this is the deterioration watch. A
        # picture that was safe at window one and is not at window two ends
        # in a retreat, whatever changed: endurance, panic, a wound, the
        # group, the weapon.
        decision = assess_engagement(observation, target_ref, self._policy)
        if decision.refused:
            refusal = decision.refusal
            assert refusal is not None  # refused decisions carry their token
            if refusal is CombatRefusal.WEAPON_UNUSABLE and not self._equip_requested:
                # The one refusal with a deterministic remedy that is not a
                # swing: draw the best carried weapon, once. If the policy
                # still refuses on the next pass, the branch below retreats.
                self._equip_requested = True
                self._stage = PHASE_EQUIP
                return self._emit(observation, ActionName.COMBAT_EQUIP_BEST, {})
            self._refusal = refusal.value
            return self._retreat_then_end(
                observation,
                ENDED_RETREATED,
                f"combat policy refused: {refusal.value} ({decision.detail})",
            )
        if self._windows >= self._limits.max_windows:
            return self._retreat_then_end(
                observation,
                ENDED_EXHAUSTED,
                f"the window budget ({self._limits.max_windows}) ran out with the "
                "target still standing",
            )
        if decision.verdict is EngagementVerdict.SHOVE_FIRST:
            self._shoves += 1
            self._stage = PHASE_SHOVE
            return self._emit(observation, ActionName.COMBAT_SHOVE, {"target_ref": target_ref})
        self._windows += 1
        self._stage = PHASE_ENGAGE
        return self._emit(observation, ActionName.COMBAT_ENGAGE, {"target_ref": target_ref})

    # -- endings -------------------------------------------------------------

    def _retreat_then_end(
        self, observation: Observation, ended: str, cause: str
    ) -> NextMissionMove:
        """Mandatory retreat, then the typed end — in that order.

        ``THREAT_INTERRUPTED`` for both retreat-first endings, because that
        is the shared fact: the goal ends with the threat unresolved. Which
        budget or gate stopped the fighting is the detail's and the report's
        job. The ending is sealed *before* the retreat runs, so nothing the
        retreat observes can talk the mission back into fighting.
        """
        self._end_after_retreat = (ended, ReasonCode.THREAT_INTERRUPTED, cause)
        self._stage = PHASE_RETREAT
        return self._emit(observation, ActionName.COMBAT_RETREAT, {})

    def _finish(self, observation: Observation) -> NextMissionMove:
        """Complete — through the last real result, or through one probe.

        The loot mission's arrangement, for its reason: a mission that ran
        no action (the target was observed already down at activation) holds
        no evidence to succeed the goal with, and minting a result would
        fabricate it. The probe is a bounded move inside the character's own
        square, numbered so a failed probe's retry never reuses a key.
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
                idempotency_key=f"combat:{self._goal_id}:done{self._probes}",
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
            idempotency_key=f"combat:{self._goal_id}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)


def _nearest(zombies: tuple[NearbyZombie, ...]) -> NearbyZombie | None:
    """The nearest observed zombie, ties broken on the reference.

    Deterministic on purpose — the same observation always pins the same
    target — and the whole of what "serve the nearest single target" means:
    the parameterless kind's one selection, made once, from observed facts.
    """
    if not zombies:
        return None
    return min(zombies, key=lambda zombie: (max(0.0, zombie.distance), zombie.ref))
