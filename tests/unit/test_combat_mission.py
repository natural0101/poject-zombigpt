"""The combat mission: policy before every window, retreat on deterioration.

Driven at the mission seam like the care and avoid missions, because these are
the promises the epic's safety constraints turn into behaviour:

* the deterministic policy is re-run before EVERY window, so deterioration
  between windows — a second zombie, a draining vital, a breaking weapon —
  switches the mission to one ``combat.retreat`` and a typed end;
* at most :data:`MAX_WINDOWS` bounded windows, each its own channel command,
  never an "attack until dead" loop;
* one target ever: pinned at the first threatened observation, never re-aimed;
* success only on the re-observed target down (or the engage adapter's
  verified gone), and the no-work case — already down at activation — served
  by the completion probe, never a fabricated result;
* a kill order with nothing observed to kill is a typed refusal, not a
  vacuous success.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_cli.combat_mission import (
    COMBAT_PHASES,
    ENDED_EXHAUSTED,
    ENDED_LOST,
    ENDED_NO_TARGET,
    ENDED_RETREATED,
    MAX_WINDOWS,
    CombatMissionLimits,
    EngageZombieMission,
)
from pz_agent_cli.loot_mission import (
    ENDED_COMPLETE,
    ENDED_NO_PROGRESS,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
)
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.combat import GROUP_RING
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    Hands,
    ItemView,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import HOME_X, HOME_Y, a_world, an_item

GOAL_ID = "3f1b0c92-0000-4000-8000-00000000000d"
WEAPON_REF = f"item:{DEFAULT_SESSION}:player-main:w1:0"
TARGET_REF = f"zombie:{DEFAULT_SESSION}:z1:0"


def a_zombie(
    rid: str = "z1",
    distance: float = 1.5,
    *,
    state: str | None = "standing",
) -> NearbyZombie:
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:{rid}:0",
        distance=distance,
        visible=True,
        chasing=False,
        position=Position(x=float(HOME_X) + distance, y=float(HOME_Y), z=0),
        state=state,
    )


def a_weapon(condition: float = 8) -> ItemView:
    return an_item(
        runtime_id="w1",
        full_type="Base.BaseballBat",
        display_name="Baseball Bat",
        category="Weapon",
        extra={"weapon": {"condition": condition, "condition_max": 10}},
    )


def world(
    seq: int = 1,
    *,
    zombies: list[NearbyZombie] | None = None,
    stats: dict[str, Any] | None = None,
    primary: str | None = WEAPON_REF,
    weapon_condition: float = 8,
) -> Observation:
    return a_world(
        seq=seq,
        items=[a_weapon(weapon_condition)],
        hands=Hands(primary=primary),
        stats=dict(stats or {}),
        nearby=NearbyView(zombies=list(zombies if zombies is not None else [a_zombie()])),
    )


def succeeded(request: ActionRequest, *, seq: int, evidence: dict[str, object]) -> ActionResult:
    return ActionResult.succeeded(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        evidence=dict(evidence),
    )


def failed(request: ActionRequest, *, seq: int, reason: ReasonCode) -> ActionResult:
    return ActionResult.failure(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        reason_code=reason,
    )


def step_of(move: object) -> ActionRequest:
    assert isinstance(move, MissionStep)
    return move.request


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


class TestTheFight:
    def test_shove_then_window_then_observed_down_completes(self) -> None:
        """Standing at contact range: shove first, one window, done on the
        re-observed prone target — never on the swing."""
        mission = EngageZombieMission(GOAL_ID)

        first = step_of(mission.next_step(world(1)))
        assert first.action is ActionName.COMBAT_SHOVE
        assert first.args == {"target_ref": TARGET_REF}
        assert first.idempotency_key == f"combat:{GOAL_ID}:s1"
        mission.note_result(
            succeeded(first, seq=2, evidence={"target_ref": TARGET_REF, "state_after": "prone"})
        )

        # The shoved zombie is back up and further out: the policy says
        # engage, and the window is its own command.
        second = step_of(mission.next_step(world(3, zombies=[a_zombie(distance=2.5)])))
        assert second.action is ActionName.COMBAT_ENGAGE
        assert second.args == {"target_ref": TARGET_REF}
        mission.note_result(
            succeeded(second, seq=4, evidence={"target_ref": TARGET_REF, "outcome": "down"})
        )

        done = mission.next_step(world(5, zombies=[a_zombie(state="prone")]))
        assert isinstance(done, MissionComplete)
        assert mission.ended == ENDED_COMPLETE
        assert mission.report["windows_fought"] == 1
        assert mission.report["shoves"] == 1

    def test_a_verified_gone_target_completes_after_a_succeeded_window(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(
            succeeded(window, seq=2, evidence={"target_ref": TARGET_REF, "outcome": "gone"})
        )
        done = mission.next_step(world(3, zombies=[]))
        assert isinstance(done, MissionComplete)

    def test_one_step_in_flight_at_a_time(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        step_of(mission.next_step(world(1)))
        assert mission.next_step(world(2)) is None

    def test_the_phases_are_the_closed_tokens(self) -> None:
        assert COMBAT_PHASES == ("start", "equip", "shove", "engage", "retreat")


# --------------------------------------------------------------------------
# the target is pinned
# --------------------------------------------------------------------------


class TestOneTargetEver:
    def test_the_nearest_zombie_is_pinned_deterministically(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        # The far one sits outside the group ring, so it is a fact, not a
        # second opponent — and not the target either.
        crowd_free = [
            a_zombie(rid="far", distance=GROUP_RING + 2.0),
            a_zombie(rid="z1", distance=3.0),
        ]
        step = step_of(mission.next_step(world(1, zombies=crowd_free)))
        assert step.args["target_ref"] == TARGET_REF  # z1, the nearer

    def test_a_second_zombie_mid_mission_is_deterioration_not_a_new_target(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(failed(window, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

        crowded = world(
            3,
            zombies=[
                a_zombie(distance=3.0),
                a_zombie(rid="z2", distance=GROUP_RING - 1.0),
            ],
        )
        retreat = step_of(mission.next_step(crowded))
        assert retreat.action is ActionName.COMBAT_RETREAT
        mission.note_result(succeeded(retreat, seq=4, evidence={"nearest_before": 3.0}))

        ended = mission.next_step(world(5, zombies=[a_zombie(distance=3.0)]))
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.THREAT_INTERRUPTED
        assert "group_over_limit" in ended.detail
        assert mission.ended == ENDED_RETREATED
        assert mission.report["refusal"] == "group_over_limit"

    def test_a_succeeded_shove_is_not_a_kill_when_the_target_vanishes(self) -> None:
        """A window was *attempted* and failed; the only action that succeeded
        is a shove, whose adapter proves "down **or further away**". The target
        then leaves observation — which is exactly what a successful shove
        makes likely — and that must read ``lost``, not ``complete``: the
        absence rule is the engage adapter's, and no engage window ever
        succeeded here."""
        mission = EngageZombieMission(GOAL_ID)
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(failed(window, seq=2, reason=ReasonCode.POSTCONDITION_FAILED))

        shove = step_of(mission.next_step(world(3, zombies=[a_zombie(distance=1.5)])))
        assert shove.action is ActionName.COMBAT_SHOVE
        mission.note_result(
            succeeded(shove, seq=4, evidence={"target_ref": TARGET_REF, "distance_after": 3.0})
        )

        ended = mission.next_step(world(5, zombies=[]))
        assert isinstance(ended, MissionRefused), f"a shoved-away zombie is not a kill: {ended!r}"
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert mission.ended == ENDED_LOST

    def test_a_succeeded_equip_is_not_a_kill_when_the_target_vanishes(self) -> None:
        """The same gap through the other door: the equip that succeeded says
        a weapon was drawn, nothing about the zombie."""
        mission = EngageZombieMission(GOAL_ID, limits=CombatMissionLimits(max_windows=1))
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(failed(window, seq=2, reason=ReasonCode.POSTCONDITION_FAILED))

        equip = step_of(mission.next_step(world(3, zombies=[a_zombie(distance=3.0)], primary=None)))
        assert equip.action is ActionName.COMBAT_EQUIP_BEST
        mission.note_result(succeeded(equip, seq=4, evidence={"item_ref": WEAPON_REF}))

        ended = mission.next_step(world(5, zombies=[]))
        assert isinstance(ended, MissionRefused), f"a drawn weapon is not a kill: {ended!r}"
        assert mission.ended == ENDED_LOST

    def test_a_target_that_leaves_unfought_ends_lost_not_complete(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        shove = step_of(mission.next_step(world(1)))
        assert shove.action is ActionName.COMBAT_SHOVE
        mission.note_result(
            succeeded(shove, seq=2, evidence={"target_ref": TARGET_REF, "distance_after": 2.5})
        )
        # No engage window has run; the target wandering out of view is not a
        # kill, however the shove went.
        ended = mission.next_step(world(3, zombies=[]))
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert mission.ended == ENDED_LOST


# --------------------------------------------------------------------------
# the policy runs before every window
# --------------------------------------------------------------------------


class TestDeteriorationRetreats:
    def run_one_window(self, mission: EngageZombieMission) -> None:
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(failed(window, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

    @pytest.mark.parametrize(
        ("stats", "token"),
        [
            ({"endurance": 0.05}, "endurance_critical"),
            ({"panic": 0.95}, "panic_critical"),
            ({"health": 0.2}, "wounded"),
            ({"fatigue": 0.95}, "endurance_critical"),
        ],
        ids=["endurance", "panic", "health", "fatigue"],
    )
    def test_a_vital_crossing_between_windows_retreats(
        self, stats: dict[str, float], token: str
    ) -> None:
        mission = EngageZombieMission(GOAL_ID)
        self.run_one_window(mission)

        move = mission.next_step(world(3, zombies=[a_zombie(distance=3.0)], stats=stats))
        retreat = step_of(move)
        assert retreat.action is ActionName.COMBAT_RETREAT
        mission.note_result(succeeded(retreat, seq=4, evidence={"nearest_before": 3.0}))

        ended = mission.next_step(world(5, zombies=[a_zombie(distance=3.0)], stats=stats))
        assert isinstance(ended, MissionRefused)
        assert token in ended.detail
        assert mission.ended == ENDED_RETREATED

    def test_the_retreat_ending_survives_a_failed_retreat(self) -> None:
        """The typed end does not depend on the retreat's own success — the
        honest picture is reported either way."""
        mission = EngageZombieMission(GOAL_ID)
        self.run_one_window(mission)
        hurt = world(3, zombies=[a_zombie(distance=3.0)], stats={"health": 0.2})
        retreat = step_of(mission.next_step(hurt))
        mission.note_result(failed(retreat, seq=4, reason=ReasonCode.CAPABILITY_UNAVAILABLE))
        ended = mission.next_step(world(5, zombies=[a_zombie(distance=3.0)]))
        assert isinstance(ended, MissionRefused)
        assert mission.ended == ENDED_RETREATED

    def test_nothing_talks_a_sealed_retreat_back_into_fighting(self) -> None:
        """A picture that recovers after the retreat was ordered stays ended:
        the ending is sealed before the retreat runs."""
        mission = EngageZombieMission(GOAL_ID)
        self.run_one_window(mission)
        hurt = world(3, zombies=[a_zombie(distance=3.0)], stats={"health": 0.2})
        retreat = step_of(mission.next_step(hurt))
        mission.note_result(succeeded(retreat, seq=4, evidence={"nearest_before": 3.0}))
        # Healthy again, target still there — and the mission still ends.
        ended = mission.next_step(world(5, zombies=[a_zombie(distance=3.0)]))
        assert isinstance(ended, MissionRefused)
        assert mission.ended == ENDED_RETREATED


# --------------------------------------------------------------------------
# the weapon path
# --------------------------------------------------------------------------


class TestTheWeaponPath:
    def test_bare_hands_earn_one_equip_before_the_policy_reruns(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        equip = step_of(mission.next_step(world(1, primary=None)))
        assert equip.action is ActionName.COMBAT_EQUIP_BEST
        assert equip.args == {}
        mission.note_result(succeeded(equip, seq=2, evidence={"item_ref": WEAPON_REF}))

        # Armed now: the same picture engages (past contact range, no shove).
        window = step_of(mission.next_step(world(3, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        assert mission.report["equip_requested"] is True

    def test_a_second_weapon_refusal_retreats_instead_of_equipping_again(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        equip = step_of(mission.next_step(world(1, primary=None)))
        assert equip.action is ActionName.COMBAT_EQUIP_BEST
        mission.note_result(failed(equip, seq=2, reason=ReasonCode.PRECONDITION_FAILED))
        # Still bare-handed: the one equip was spent, so the refusal now
        # follows the mandatory-retreat rule like every other.
        retreat = step_of(mission.next_step(world(3, primary=None)))
        assert retreat.action is ActionName.COMBAT_RETREAT
        mission.note_result(succeeded(retreat, seq=4, evidence={"nearest_before": 1.5}))
        ended = mission.next_step(world(5, primary=None))
        assert isinstance(ended, MissionRefused)
        assert "weapon_unusable" in ended.detail
        assert mission.ended == ENDED_RETREATED


# --------------------------------------------------------------------------
# the window budget
# --------------------------------------------------------------------------


class TestTheWindowBudget:
    def test_the_windows_are_bounded_and_the_next_move_is_the_retreat(self) -> None:
        """Exactly the budgeted windows are fought, then one retreat.

        Two windows under a narrowed budget, both failing inside the streak
        cap: the third pass through the mission emits a retreat, never a
        third window — the "attack until dead" loop is structurally absent.
        """
        mission = EngageZombieMission(GOAL_ID, limits=CombatMissionLimits(max_windows=2))
        picture = [a_zombie(distance=3.0)]
        for seq in range(2):
            window = step_of(mission.next_step(world(seq * 2 + 1, zombies=picture)))
            assert window.action is ActionName.COMBAT_ENGAGE
            mission.note_result(
                failed(window, seq=seq * 2 + 2, reason=ReasonCode.POSTCONDITION_FAILED)
            )
        third = step_of(mission.next_step(world(5, zombies=picture)))
        assert third.action is ActionName.COMBAT_RETREAT
        assert mission.report["windows_fought"] == 2

    def test_windows_exhausted_ends_exhausted_after_a_retreat(self) -> None:
        mission = EngageZombieMission(GOAL_ID, limits=CombatMissionLimits(max_windows=1))
        window = step_of(mission.next_step(world(1, zombies=[a_zombie(distance=3.0)])))
        assert window.action is ActionName.COMBAT_ENGAGE
        mission.note_result(failed(window, seq=2, reason=ReasonCode.POSTCONDITION_FAILED))

        retreat = step_of(mission.next_step(world(3, zombies=[a_zombie(distance=3.0)])))
        assert retreat.action is ActionName.COMBAT_RETREAT
        mission.note_result(succeeded(retreat, seq=4, evidence={"nearest_before": 3.0}))

        ended = mission.next_step(world(5, zombies=[a_zombie(distance=3.0)]))
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.THREAT_INTERRUPTED
        assert mission.ended == ENDED_EXHAUSTED

    def test_the_limits_ceiling_cannot_be_raised(self) -> None:
        with pytest.raises(ValueError, match="max_windows"):
            CombatMissionLimits(max_windows=MAX_WINDOWS + 1)

    def test_consecutive_failures_end_no_progress_without_a_retreat(self) -> None:
        """Channel-level failures are not deterioration: a retreat would be a
        fourth command into whatever refused the first three."""
        mission = EngageZombieMission(GOAL_ID)
        picture = [a_zombie(distance=3.0)]
        for seq in range(3):
            window = step_of(mission.next_step(world(seq * 2 + 1, zombies=picture)))
            mission.note_result(
                failed(window, seq=seq * 2 + 2, reason=ReasonCode.CAPABILITY_UNAVAILABLE)
            )
        ended = mission.next_step(world(9, zombies=picture))
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert mission.ended == ENDED_NO_PROGRESS


# --------------------------------------------------------------------------
# the edges
# --------------------------------------------------------------------------


class TestTheEdges:
    def test_no_zombie_at_activation_is_a_typed_refusal(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        ended = mission.next_step(world(1, zombies=[]))
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert mission.ended == ENDED_NO_TARGET

    def test_a_target_already_down_completes_through_the_probe(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        move = mission.next_step(world(1, zombies=[a_zombie(state="prone")]))
        assert isinstance(move, MissionProbe)
        assert move.request.action is ActionName.MOVEMENT_MOVE_TO
        assert move.request.idempotency_key == f"combat:{GOAL_ID}:done1"
        assert mission.ended == ENDED_COMPLETE
        assert mission.report["windows_fought"] == 0

    def test_a_missing_nearby_tier_is_a_typed_refusal(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        blind = a_world(seq=1, no_nearby=True, hands=Hands(primary=WEAPON_REF))
        ended = mission.next_step(blind)
        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE

    def test_a_sealed_ending_replays(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        first = mission.next_step(world(1, zombies=[]))
        second = mission.next_step(world(2))
        assert first is second

    def test_mark_abandoned_seals_only_a_mission_in_flight(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        step_of(mission.next_step(world(1)))
        mission.mark_abandoned()
        assert mission.ended == "cancelled"
        finished = EngageZombieMission(GOAL_ID)
        finished.next_step(world(1, zombies=[]))
        finished.mark_abandoned()
        assert finished.ended == ENDED_NO_TARGET

    def test_the_summary_line_is_constants_and_counts_only(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        mission.next_step(world(1, zombies=[]))
        line = mission.summary_line()
        assert line == "combat no_target: windows=0 shoves=0 refusal=none"
        assert "\n" not in line and DEFAULT_SESSION not in line

    def test_results_for_foreign_actions_are_ignored(self) -> None:
        mission = EngageZombieMission(GOAL_ID)
        step = step_of(mission.next_step(world(1)))
        foreign = succeeded(
            ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=step.session_id,
                idempotency_key="someone-else",
                args={},
            ),
            seq=2,
            evidence={"position": {"x": 1}},
        )
        mission.note_result(foreign)
        # Still waiting on its own step: the foreign result changed nothing.
        assert mission.next_step(world(3)) is None
