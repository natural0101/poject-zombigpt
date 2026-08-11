"""``treat_wounds``, ``rest_until``, ``sleep_until_rested``: the care missions.

The goal channel's care wave, three deterministic drives over the existing
medical and survival adapters. One file houses all three on purpose: they
share the mission contract, the probe arrangement and the bookkeeping, and a
``medical_mission.py`` beside a survival pair served inline would be two
spellings of the same seam for the wrapper to drift between. Each drive is a
state machine over observations, shaped exactly like
:class:`~pz_agent_cli.loot_mission.LootMission` and
:class:`~pz_agent_cli.explore_mission.ExploreMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` can drive all of them through one
seam, and each answers with the *same* frozen move vocabulary — imported from
:mod:`pz_agent_cli.loot_mission` rather than restated, so the wrapper cannot
come to route the missions' moves differently:

* :class:`~pz_agent_cli.loot_mission.MissionStep` travels the loop's
  **action channel** — every ``inventory.ensure_main``, ``medical.bandage``,
  ``survival.rest`` and ``survival.sleep``. The adapters do the proving:
  a bandage succeeds only when the wound was observed to stop bleeding, a
  rest only when endurance was observed at or above the target, a sleep only
  when fatigue was observed falling over slept world hours. No mission here
  re-implements a verification, and none may end the goal off a step that is
  not the goal's own postcondition.
* :class:`~pz_agent_cli.loot_mission.MissionProbe` travels the **goal
  seam**, emitted only by a mission that finished without a single action
  running — nothing was bleeding at activation, or endurance already stood
  at the target. Nothing exists to succeed the goal with, and minting a
  result would fabricate evidence; the probe is a bounded move inside the
  character's own square whose *observed* success the loop settles the goal
  with, exactly the loot and explore arrangement.
* :class:`~pz_agent_cli.loot_mission.MissionComplete` and
  :class:`~pz_agent_cli.loot_mission.MissionRefused` end the goal through
  the queue — success on the last real channel result's evidence, refusal
  with a typed reason and a one-line report summary.

Honesty rules this module keeps:

* **The mission never picks the dressing.** Which wound and which dressing
  are :func:`pz_agent_core.policy.medical.select_treatment`'s decisions,
  re-made against every fresh observation — triage order, the dirty-dressing
  rule and the user's reserves included. This module only carries the choice
  into a command.
* **Adapter refusals ride through typed and unchanged.** The sleep adapter's
  danger refusal (``POLICY_DENIED`` while the guard reports any danger at
  all) becomes the goal's own typed failure with the same reason code, and
  the mission never re-sends a sleep behind it: a refusal the guard made is
  not a transient to retry into.
* **Complete means observed complete.** ``treat_wounds`` finishes only when
  the newest observation reports no bleeding wound; ``rest_until`` and
  ``sleep_until_rested`` finish only on the adapter's own evidence-carrying
  success. Dressings running out with wounds still bleeding is the typed
  partial failure, with the honest count in the detail.
* **Bounded everything.** Wounds per mission, consecutive failures,
  completion probes, one survival action per goal, a bounded bed scan — and
  the goal's wall clock above all of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.adapters.survival import (
    MAX_REST_TARGET,
    MAX_SLEEP_HOURS,
    MIN_REST_TARGET,
    MIN_SLEEP_HOURS,
)
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.navigation.local_map import GridSquare, square_of
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.medical import select_treatment
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    JsonDict,
    Observation,
    ReasonCode,
    SquareRef,
)

from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    PHASE_START,
    PHASE_TRANSFER,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "ENDED_EXHAUSTED",
    "ENDED_REFUSED",
    "MAX_BED_CANDIDATES",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_WOUNDS_PER_MISSION",
    "PHASE_REST",
    "PHASE_SLEEP",
    "PHASE_TREAT",
    "REST_PHASES",
    "SLEEP_PHASES",
    "TREAT_PHASES",
    "CareMissionLimits",
    "RestMission",
    "SleepMission",
    "TreatWoundsMission",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Distinct wounds one ``treat_wounds`` mission will start dressing. A body
#: has seventeen parts and blood loss has a clock; a character bleeding from
#: more than eight is a mission that ends honestly short and is resubmitted,
#: not one that dresses without end.
MAX_WOUNDS_PER_MISSION: Final = 8

#: Consecutive channel-action failures before a mission ends ``no_progress``.
#: The loot mission's own number, restated because the missions are allowed
#: to diverge and a shared constant would hide the review when one does.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no action may spend, exactly the
#: loot mission's arrangement: each probe is one goal-seam request, so the
#: goal's own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: Nearby objects one sleep mission will scan for a bed, mirroring the loot
#: mission's discovery bound: the mod bounds the tier, and this seam stays
#: bounded by its own number rather than by trust in the sender's.
MAX_BED_CANDIDATES: Final = 64

#: How a bed names itself among nearby objects. A substring on purpose —
#: the tier forwards the game's own object names lowercased — and honest
#: because it only chooses which *square* to name: the sleep adapter and the
#: mod's own bed lookup on that square re-verify that a bed actually stands
#: there, so a false match costs a typed refusal, never a fabricated night.
_BED_MARKER: Final = "bed"


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

#: Dressings ran out with wounds still bleeding: the honest partial ending.
ENDED_EXHAUSTED: Final = "exhausted"

#: The one channel action a rest or sleep mission sends was refused, and the
#: refusal's own reason code travelled to the goal unchanged.
ENDED_REFUSED: Final = "refused"

#: The per-drive pipeline phases. ``start`` and ``transfer`` are the loot
#: mission's own tokens, imported rather than restated so the wrapper cannot
#: come to read two missions' phases differently; the three below are minted
#: here, closed tokens like every phase this project publishes.
PHASE_TREAT: Final = "treat"
PHASE_REST: Final = "rest"
PHASE_SLEEP: Final = "sleep"

TREAT_PHASES: Final = (PHASE_START, PHASE_TRANSFER, PHASE_TREAT)
REST_PHASES: Final = (PHASE_START, PHASE_REST)
SLEEP_PHASES: Final = (PHASE_START, PHASE_SLEEP)


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CareMissionLimits:
    """Every bound one care mission runs under. All of them are load-bearing."""

    max_wounds: int = MAX_WOUNDS_PER_MISSION
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        for name in ("max_wounds", "max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# shared bookkeeping
# --------------------------------------------------------------------------


def _reported_stat(observation: Observation, key: str) -> float | None:
    """One stat as a number, or None when the mod did not report it.

    The survival adapters read stats the same way for the same reason:
    "not reported" and "zero" are different facts, and
    :class:`~pz_agent_core.protocol.PlayerState`'s convenience properties
    read the first as the second.
    """
    if key not in observation.player.stats:
        return None
    value = observation.player.stats[key]
    if value is None or isinstance(value, bool):
        return None
    return float(value)


class _CareDrive:
    """The bookkeeping every care drive shares.

    Not a public seam: the wrapper types against the three concrete missions,
    and this class only keeps the three of them from spelling "one step in
    flight, a sealed final answer, a bounded probe" three slightly different
    ways.
    """

    def __init__(self, goal_id: str, *, key_prefix: str, limits: CareMissionLimits) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        self._goal_id = goal_id
        self._key_prefix = key_prefix
        self._limits = limits
        self._stage = PHASE_START
        self._pending_action: str | None = None
        self._steps = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False
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
        """Where the drive stands: a closed token, publishable verbatim.

        ``start`` covers "not yet begun" and "between actions"; a mission that
        already ended keeps the phase it stopped in, because whether it ended
        is :attr:`ended`'s answer, not this one's.
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

    # -- shared machinery ---------------------------------------------------

    def _emit(self, observation: Observation, action: ActionName, args: JsonDict) -> MissionStep:
        self._steps += 1
        request = ActionRequest(
            action=action,
            session_id=observation.session_id,
            idempotency_key=f"{self._key_prefix}:{self._goal_id}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)

    def _finish(self, observation: Observation) -> NextMissionMove:
        """Complete — through the last real result, or through one probe.

        The loot mission's own arrangement, for the loot mission's own
        reason: a mission that ran no action holds no evidence to succeed
        the goal with, and minting a result would fabricate it. The probe is
        the engine observing an ordinary fact instead — a bounded move inside
        the square the character already stands on — numbered so a failed
        probe's retry never reuses a key.
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
                idempotency_key=f"{self._key_prefix}:{self._goal_id}:done{self._probes}",
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

    def summary_line(self) -> str:
        """One bounded line for a goal record's ``detail``. Overridden per drive."""
        return f"{self._key_prefix} {self._ended}"


# --------------------------------------------------------------------------
# treat_wounds
# --------------------------------------------------------------------------


class TreatWoundsMission(_CareDrive):
    """One ``treat_wounds`` goal: dress every observed bleeding wound.

    Created by the wrapper when the goal activates and discarded when the
    goal reaches a terminal state — the mission never outlives its goal. The
    provable criterion is read off the newest observation: **no observed
    wound bleeds**. Every choice in between — which wound first, which
    dressing, whether a dirty one may be used, whether a reserved one may be
    spent — is :func:`~pz_agent_core.policy.medical.select_treatment`'s,
    re-made per observation, and the bandage adapter proves each dressing by
    the wound it was sent to no longer being reported bleeding.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
        limits: CareMissionLimits | None = None,
    ) -> None:
        super().__init__(
            goal_id,
            key_prefix="treat",
            limits=limits if limits is not None else CareMissionLimits(),
        )
        self._policy = policy
        #: Body parts whose dressing pipeline began — the wound budget's unit.
        self._wounds_started: set[str] = set()
        #: Body parts whose bandage the engine verified. Closed tokens: a
        #: success means the adapter accepted the part against its own
        #: BODY_PARTS list, so the report may publish them verbatim.
        self._bandaged: list[str] = []
        #: The part the in-flight bandage addresses, recorded on its success.
        self._pending_body_part: str | None = None
        #: Bleeding wounds in the newest observation, for the report.
        self._bleeding_observed = 0

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
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True
            self._consecutive_failures = 0
            if self._stage == PHASE_TREAT and self._pending_body_part is not None:
                # The adapter's verify observed the bleeding stop; only now
                # is the part counted as dressed.
                self._bandaged.append(self._pending_body_part)
        else:
            self._consecutive_failures += 1
        self._pending_body_part = None
        self._stage = PHASE_START

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: replay a sealed ending, wait out an in-flight step,
        stop a failing mission, read the criterion, then let the policy pick
        — or finish. Bounded by the wound budget and the failure streak.
        """
        if self._final is not None:
            return self._final
        self._bleeding_observed = sum(1 for wound in observation.player.wounds if wound.bleeding)
        if self._pending_action is not None:
            return None
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive sub-actions failed",
            )
        if self._bleeding_observed == 0:
            # The provable criterion, read off the observation itself. At
            # activation this is the no-work completion the probe serves.
            return self._finish(observation)
        inventory = observation.inventory
        if inventory is None:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod sent no inventory this tick, so no dressing can be found",
            )
        selection = select_treatment(inventory, observation.player, self._policy)
        choice = selection.choice
        if choice is None:
            if selection.reason_code is ReasonCode.PRECONDITION_FAILED:
                # Wounds bleed, yet the triage found none it can name a body
                # part for — nothing here could ever be verified dressed.
                return self._refuse(
                    ENDED_NO_PROGRESS,
                    ReasonCode.PRECONDITION_FAILED,
                    f"{self._bleeding_observed} wound(s) bleed but none names a "
                    "treatable body part",
                )
            # The honest partial ending the directive names: dressings exist
            # only as refused candidates (reserved, unreachable, equipped) or
            # not at all, and the count says what remains.
            return self._refuse(
                ENDED_EXHAUSTED,
                ReasonCode.PRECONDITION_FAILED,
                f"{self._bleeding_observed} wound(s) still bleed and no bandage remains",
            )
        if (
            choice.body_part not in self._wounds_started
            and len(self._wounds_started) >= self._limits.max_wounds
        ):
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"the wound budget ({self._limits.max_wounds}) ran out with wounds still bleeding",
            )
        self._wounds_started.add(choice.body_part)
        if choice.needs_transfer_to_main:
            # The bandage adapter requires the dressing in the main
            # inventory; the move is its own observed action, and the next
            # observation re-selects against where things then are.
            self._stage = PHASE_TRANSFER
            return self._emit(
                observation,
                ActionName.INVENTORY_ENSURE_MAIN,
                {"item_ref": choice.item_ref},
            )
        self._stage = PHASE_TREAT
        self._pending_body_part = choice.body_part
        return self._emit(
            observation,
            ActionName.MEDICAL_BANDAGE,
            {"body_part": choice.body_part, "item_ref": choice.item_ref},
        )

    # -- the deliverable -----------------------------------------------------

    @property
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        ``wounds_bandaged`` carries only parts whose bandage the engine
        verified — members of the adapter's own closed BODY_PARTS list, so
        the report holds no game-authored text.
        """
        return {
            "wounds_bandaged": list(self._bandaged),
            "wounds_started": len(self._wounds_started),
            "bleeding_remaining": self._bleeding_observed,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """Constants and counts only, per the goal channel's detail rule."""
        return (
            f"treat {self._ended}: bandaged={len(self._bandaged)} "
            f"bleeding={self._bleeding_observed}"
        )


# --------------------------------------------------------------------------
# rest_until
# --------------------------------------------------------------------------


class RestMission(_CareDrive):
    """One ``rest_until`` goal: one ``survival.rest`` to a stated target.

    The adapter owns the waiting (its own wall-clock bounds) and the proof
    (endurance observed at or above the target, higher than it was); the
    mission owns exactly two decisions — "already there" completes without
    work through the probe, and any adapter refusal rides to the goal typed
    and unchanged, never retried.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        target_endurance: float,
        limits: CareMissionLimits | None = None,
    ) -> None:
        super().__init__(
            goal_id,
            key_prefix="rest",
            limits=limits if limits is not None else CareMissionLimits(),
        )
        if not MIN_REST_TARGET <= target_endurance <= MAX_REST_TARGET:
            # The channel ranges the parameter with the adapter's own bounds;
            # a mission built around them is a programming error, not a goal.
            raise ValueError(
                f"target_endurance must be within {MIN_REST_TARGET}..{MAX_REST_TARGET}, "
                f"got {target_endurance}"
            )
        self._target = target_endurance
        self._requested = False
        self._outcome: ActionResult | None = None
        self._endurance_observed: float | None = None

    def note_result(self, result: ActionResult) -> None:
        """Fold the one terminal result the mission ever waits for."""
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        self._outcome = result
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True

    def next_step(self, observation: Observation) -> NextMissionMove:
        if self._final is not None:
            return self._final
        self._endurance_observed = _reported_stat(observation, "endurance")
        if self._pending_action is not None:
            return None
        outcome = self._outcome
        if outcome is not None:
            if outcome.status is ActionStatus.SUCCEEDED:
                # The adapter's evidence is the observed postcondition:
                # endurance at or above the target, and above where it was.
                self._ended = ENDED_COMPLETE
                self._final = MissionComplete()
                return self._final
            # Typed and unchanged: the adapter's reason code is the goal's.
            return self._refuse(
                ENDED_REFUSED,
                outcome.reason_code,
                f"survival.rest refused: {outcome.reason_code.value}",
            )
        if self._endurance_observed is not None and self._endurance_observed >= self._target:
            # Already at the target: the adapter would refuse the command as
            # already met, and rightly — the no-work completion is the
            # probe's, exactly like a treat mission over an unbleeding body.
            return self._finish(observation)
        self._requested = True
        self._stage = PHASE_REST
        return self._emit(
            observation,
            ActionName.SURVIVAL_REST,
            {"target_endurance": self._target},
        )

    @property
    def report(self) -> JsonDict:
        return {
            "target_endurance": self._target,
            "endurance_observed": self._endurance_observed,
            "requested": self._requested,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        return f"rest {self._ended}: target={self._target:.2f}"


# --------------------------------------------------------------------------
# sleep_until_rested
# --------------------------------------------------------------------------


class SleepMission(_CareDrive):
    """One ``sleep_until_rested`` goal: one ``survival.sleep`` on an observed bed.

    The adapter owns every proof (fatigue observed falling over slept world
    hours) and every guard — above all the danger refusal: sleep is the one
    action the mod cannot interrupt, so the adapter refuses it outright
    while the guard reports any danger at all, and this mission carries that
    refusal to the goal typed and unchanged, never retrying into it. The
    mission's own two decisions are which observed bed square to name and
    whether to pass the goal's ``hours`` — an absent value stays absent, so
    the adapter's own default night applies without being restated here.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        hours: int | None = None,
        limits: CareMissionLimits | None = None,
    ) -> None:
        super().__init__(
            goal_id,
            key_prefix="sleep",
            limits=limits if limits is not None else CareMissionLimits(),
        )
        if hours is not None and not MIN_SLEEP_HOURS <= hours <= MAX_SLEEP_HOURS:
            raise ValueError(
                f"hours must be within {MIN_SLEEP_HOURS}..{MAX_SLEEP_HOURS}, got {hours}"
            )
        self._hours = hours
        self._requested = False
        self._outcome: ActionResult | None = None
        self._bed_ref: str | None = None

    def note_result(self, result: ActionResult) -> None:
        """Fold the one terminal result the mission ever waits for."""
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        self._outcome = result
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True

    def next_step(self, observation: Observation) -> NextMissionMove:
        if self._final is not None:
            return self._final
        if self._pending_action is not None:
            return None
        outcome = self._outcome
        if outcome is not None:
            if outcome.status is ActionStatus.SUCCEEDED:
                # The adapter verified fatigue fell over enough world clock
                # to be a slept night rather than a quiet afternoon.
                self._ended = ENDED_COMPLETE
                self._final = MissionComplete()
                return self._final
            # The danger refusal — and every other adapter refusal — ends
            # the goal with the adapter's own reason code. Sealing _final
            # here is what makes "never retried into danger" structural: a
            # later observation replays the refusal instead of re-sending.
            return self._refuse(
                ENDED_REFUSED,
                outcome.reason_code,
                f"survival.sleep refused: {outcome.reason_code.value}",
            )
        bed = self._find_bed(observation)
        if bed is None:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.PRECONDITION_FAILED,
                "no bed is observed nearby; stand where one is visible and resubmit",
            )
        self._bed_ref = bed
        args: JsonDict = {"bed_ref": bed}
        if self._hours is not None:
            args["hours"] = self._hours
        self._requested = True
        self._stage = PHASE_SLEEP
        return self._emit(observation, ActionName.SURVIVAL_SLEEP, args)

    def _find_bed(self, observation: Observation) -> str | None:
        """The square reference of the nearest observed bed, or None.

        A bounded scan over the nearby tier for an object whose kind or
        semantics name a bed, resolved to the *square* the sleep command
        takes. Deterministic: candidates order by distance from the
        character's square, then by reference, so the same observation
        always names the same bed. The match only chooses a square — the
        mod's own bed lookup on it is what decides a bed truly stands there.
        """
        nearby = observation.nearby
        if nearby is None:
            return None
        here = square_of(observation.player.position)
        found: list[tuple[int, str, GridSquare]] = []
        for seen in nearby.objects[:MAX_BED_CANDIDATES]:
            if seen.position is None:
                continue
            named = _BED_MARKER in seen.kind.casefold() or any(
                _BED_MARKER in token.casefold() for token in seen.semantics
            )
            if not named:
                continue
            square = square_of(seen.position)
            distance = max(abs(square[0] - here[0]), abs(square[1] - here[1]))
            found.append((distance, seen.ref, square))
        if not found:
            return None
        _, _, square = min(found)
        return str(
            SquareRef(
                session_id=observation.session_id,
                x=square[0],
                y=square[1],
                z=square[2],
            )
        )

    @property
    def report(self) -> JsonDict:
        return {
            "hours": self._hours,
            "bed_named": self._bed_ref is not None,
            "requested": self._requested,
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        hours = "adapter default" if self._hours is None else str(self._hours)
        return f"sleep {self._ended}: hours={hours}"
