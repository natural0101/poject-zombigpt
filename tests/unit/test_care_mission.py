"""The care missions: treat_wounds, rest_until and sleep_until_rested.

Driven at the mission seam — the same frozen move vocabulary the loot and
explore missions answer the wrapper with — because the wrapper routing for
the care kinds is the next wave's change and these promises hold below it:

* ``treat_wounds`` dresses every observed bleeding wound worst-first through
  ``medical.bandage``, counts a part as dressed only after the engine's
  verified success, and completes only when the newest observation reports
  no bleeding wound at all;
* no bleeding at activation is the no-work completion, served by the same
  bounded probe the loot mission mints — never by a fabricated result;
* dressings running out with wounds still bleeding is the typed partial
  failure ``PRECONDITION_FAILED`` with the honest count in the detail;
* the user's reserves are the medical policy's to respect, and the mission
  never routes around them;
* ``rest_until`` sends one ``survival.rest`` and completes on the adapter's
  observed-endurance success; a target already met completes without work;
* ``sleep_until_rested`` sends one ``survival.sleep`` on an observed bed,
  surfaces the adapter's danger refusal typed and *unchanged*, and never
  retries into it; its success is the adapter's fatigue-fell evidence.
"""

from __future__ import annotations

import pytest

from pz_agent_cli.care_mission import (
    ENDED_EXHAUSTED,
    ENDED_REFUSED,
    MAX_COMPLETION_PROBES,
    MAX_WOUNDS_PER_MISSION,
    PHASE_SLEEP,
    PHASE_TREAT,
    CareMissionLimits,
    RestMission,
    SleepMission,
    TreatWoundsMission,
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
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ContainerKind,
    InventoryView,
    ItemView,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
    Wound,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    main_container_ref,
    make_container,
    make_item,
    make_observation,
    make_player,
)

MAIN = main_container_ref()
BACKPACK = backpack_container_ref()

GOAL_ID = "3f1b0c92-0000-4000-8000-00000000000c"


# --------------------------------------------------------------------------
# the world under the missions
# --------------------------------------------------------------------------


def wound(part: str, *, bleeding: bool = True, severity: float = 0.5) -> Wound:
    return Wound(
        ref=f"wound:{DEFAULT_SESSION}:{part}",
        kind="scratch",
        severity=severity,
        bleeding=bleeding,
    )


def bandage(ref_id: str, *, container: str = MAIN, **overrides: object) -> ItemView:
    return make_item(
        f"item:{DEFAULT_SESSION}:tail:{ref_id}:0",
        container,
        full_type="Base.Bandage",
        display_name="Bandage",
        category="FirstAid",
        weight=0.1,
        **overrides,
    )


def a_bed(x: int = 1201, y: int = 3400) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:7001:0",
        kind="bed",
        distance=1.0,
        position=Position(x=float(x), y=float(y), z=0),
        semantics=["bed"],
    )


def world(
    seq: int,
    *,
    wounds: tuple[Wound, ...] = (),
    items: tuple[ItemView, ...] = (),
    stats: dict[str, float] | None = None,
    objects: tuple[NearbyObject, ...] = (),
    inventory_reported: bool = True,
) -> Observation:
    inventory = InventoryView(
        containers=[
            make_container(MAIN, ContainerKind.PLAYER_MAIN),
            make_container(BACKPACK, ContainerKind.WORN),
        ],
        items=list(items),
    )
    return make_observation(
        seq=seq,
        player=make_player(wounds=list(wounds), stats=stats or {}),
        inventory=inventory if inventory_reported else None,
        nearby=NearbyView(objects=list(objects)),
    )


def step_of(move: object) -> ActionRequest:
    assert isinstance(move, MissionStep), move
    return move.request


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


# --------------------------------------------------------------------------
# treat_wounds
# --------------------------------------------------------------------------


class TestTreatWounds:
    def test_two_bleeding_wounds_are_bandaged_worst_first_then_complete(self) -> None:
        """The happy path: each wound dressed, each verified, done on none."""
        mission = TreatWoundsMission(GOAL_ID)
        both = (wound("Head", severity=0.3), wound("ForeArm_L", severity=0.8))
        two = (bandage("b1"), bandage("b2"))

        first = step_of(mission.next_step(world(1, wounds=both, items=two)))
        # Triage is the policy's: the deeper bleeder first, whatever the order
        # the wounds arrived in.
        assert first.action is ActionName.MEDICAL_BANDAGE
        assert first.args["body_part"] == "ForeArm_L"
        assert mission.phase == PHASE_TREAT
        # A step in flight: the mission holds rather than double-submitting.
        assert mission.next_step(world(2, wounds=both, items=two)) is None

        mission.note_result(
            succeeded(first, seq=3, evidence={"bleeding_before": True, "bleeding_after": False})
        )
        remaining = (wound("Head", severity=0.3),)
        second = step_of(mission.next_step(world(4, wounds=remaining, items=(bandage("b2"),))))
        assert second.action is ActionName.MEDICAL_BANDAGE
        assert second.args["body_part"] == "Head"
        assert second.idempotency_key != first.idempotency_key
        mission.note_result(
            succeeded(second, seq=5, evidence={"bleeding_before": True, "bleeding_after": False})
        )

        done = mission.next_step(world(6, wounds=(), items=()))
        assert isinstance(done, MissionComplete)
        assert mission.ended == ENDED_COMPLETE
        assert mission.report == {
            "wounds_bandaged": ["ForeArm_L", "Head"],
            "wounds_started": 2,
            "bleeding_remaining": 0,
            "ended": ENDED_COMPLETE,
        }
        assert mission.summary_line() == "treat complete: bandaged=2 bleeding=0"

    def test_a_wound_that_stopped_on_its_own_still_counts_toward_completion(self) -> None:
        """The criterion is the observation's, not the mission's bookkeeping."""
        mission = TreatWoundsMission(GOAL_ID)
        first = step_of(
            mission.next_step(world(1, wounds=(wound("Head"),), items=(bandage("b1"),)))
        )
        mission.note_result(
            succeeded(first, seq=2, evidence={"bleeding_before": True, "bleeding_after": False})
        )
        # The wound list may keep reporting the dressed wound, no longer
        # bleeding; that is still "no observed wound bleeds".
        calm = (wound("Head", bleeding=False),)
        assert isinstance(mission.next_step(world(3, wounds=calm, items=())), MissionComplete)

    def test_no_bleeding_at_activation_completes_without_work_via_the_probe(self) -> None:
        """No action ran, so no evidence exists; the probe is the honest route."""
        mission = TreatWoundsMission(GOAL_ID)

        move = mission.next_step(world(1, wounds=(wound("Head", bleeding=False),)))

        assert isinstance(move, MissionProbe)
        assert move.request.action is ActionName.MOVEMENT_MOVE_TO
        assert move.request.idempotency_key == f"treat:{GOAL_ID}:done1"
        assert mission.any_success is False
        assert mission.ended == ENDED_COMPLETE

    def test_the_probe_is_bounded_and_exhaustion_is_typed(self) -> None:
        mission = TreatWoundsMission(GOAL_ID)
        for attempt in range(1, MAX_COMPLETION_PROBES + 1):
            move = mission.next_step(world(attempt))
            assert isinstance(move, MissionProbe)
            assert move.request.idempotency_key.endswith(f"done{attempt}")
        refused = mission.next_step(world(9))
        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.NO_PROGRESS

    def test_bandages_exhausted_is_the_typed_partial_failure(self) -> None:
        """One dressed, one still bleeding, nothing left: the honest report."""
        mission = TreatWoundsMission(GOAL_ID)
        both = (wound("Head", severity=0.3), wound("ForeArm_L", severity=0.8))
        first = step_of(mission.next_step(world(1, wounds=both, items=(bandage("b1"),))))
        mission.note_result(
            succeeded(first, seq=2, evidence={"bleeding_before": True, "bleeding_after": False})
        )

        refused = mission.next_step(world(3, wounds=(wound("Head", severity=0.3),), items=()))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "1 wound(s) still bleed and no bandage remains" in refused.detail
        assert mission.ended == ENDED_EXHAUSTED
        report = mission.report
        assert report["wounds_bandaged"] == ["ForeArm_L"]
        assert report["bleeding_remaining"] == 1
        # The ending replays; the mission never invents a new move after it.
        assert mission.next_step(world(4, wounds=(wound("Head"),))) == refused

    def test_a_reserved_dressing_is_never_spent(self) -> None:
        """Reserves are the policy's to respect and the mission rides its answer."""
        mission = TreatWoundsMission(GOAL_ID)
        reserved_only = (bandage("b1", favorite=True),)

        refused = mission.next_step(world(1, wounds=(wound("Head"),), items=reserved_only))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "no bandage remains" in refused.detail

    def test_a_clean_unreserved_dressing_is_chosen_over_a_reserved_one(self) -> None:
        mission = TreatWoundsMission(GOAL_ID)
        items = (bandage("kept", favorite=True), bandage("spend"))

        request = step_of(mission.next_step(world(1, wounds=(wound("Head"),), items=items)))

        assert request.args["item_ref"] == f"item:{DEFAULT_SESSION}:tail:spend:0"

    def test_a_dressing_out_of_main_is_transferred_first_then_used(self) -> None:
        """The bandage adapter requires main inventory; the move is observed too."""
        mission = TreatWoundsMission(GOAL_ID)
        carried = (bandage("b1", container=BACKPACK),)

        move = step_of(mission.next_step(world(1, wounds=(wound("Head"),), items=carried)))
        assert move.action is ActionName.INVENTORY_ENSURE_MAIN
        assert move.args == {"item_ref": f"item:{DEFAULT_SESSION}:tail:b1:0"}
        mission.note_result(succeeded(move, seq=2, evidence={"container_ref": MAIN}))

        # The fresh observation reports the dressing in main; only now does
        # the bandage go out.
        fresh = world(3, wounds=(wound("Head"),), items=(bandage("b1"),))
        treat = step_of(mission.next_step(fresh))
        assert treat.action is ActionName.MEDICAL_BANDAGE
        assert treat.args["body_part"] == "Head"

    def test_three_consecutive_failures_end_the_mission_typed(self) -> None:
        mission = TreatWoundsMission(GOAL_ID)
        hurt = (wound("Head"),)
        for seq in (1, 3, 5):
            request = step_of(mission.next_step(world(seq, wounds=hurt, items=(bandage("b1"),))))
            mission.note_result(failed(request, seq=seq + 1, reason=ReasonCode.ACTION_TIMEOUT))
        refused = mission.next_step(world(7, wounds=hurt, items=(bandage("b1"),)))
        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.NO_PROGRESS
        assert mission.ended == ENDED_NO_PROGRESS

    def test_the_wound_budget_bounds_the_mission(self) -> None:
        mission = TreatWoundsMission(GOAL_ID, limits=CareMissionLimits(max_wounds=1))
        both = (wound("Head", severity=0.3), wound("ForeArm_L", severity=0.8))
        stocked = world(1, wounds=both, items=(bandage("b1"), bandage("b2")))
        first = step_of(mission.next_step(stocked))
        mission.note_result(
            succeeded(first, seq=2, evidence={"bleeding_before": True, "bleeding_after": False})
        )

        refused = mission.next_step(world(3, wounds=(wound("Head"),), items=(bandage("b2"),)))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.NO_PROGRESS
        assert "wound budget (1)" in refused.detail
        assert MAX_WOUNDS_PER_MISSION == 8

    def test_a_missing_inventory_is_a_typed_refusal_not_a_guess(self) -> None:
        mission = TreatWoundsMission(GOAL_ID)
        refused = mission.next_step(world(1, wounds=(wound("Head"),), inventory_reported=False))
        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# rest_until
# --------------------------------------------------------------------------


class TestRestUntil:
    def test_one_rest_action_and_completion_on_the_observed_success(self) -> None:
        mission = RestMission(GOAL_ID, target_endurance=0.8)

        request = step_of(mission.next_step(world(1, stats={"endurance": 0.3})))
        assert request.action is ActionName.SURVIVAL_REST
        assert request.args == {"target_endurance": 0.8}
        # Still in flight: nothing else is submitted.
        assert mission.next_step(world(2, stats={"endurance": 0.5})) is None

        mission.note_result(
            succeeded(
                request,
                seq=3,
                evidence={
                    "endurance_before": 0.3,
                    "endurance_after": 0.85,
                    "target_endurance": 0.8,
                },
            )
        )
        done = mission.next_step(world(4, stats={"endurance": 0.85}))
        assert isinstance(done, MissionComplete)
        assert mission.ended == ENDED_COMPLETE
        assert mission.report["endurance_observed"] == 0.85

    def test_a_target_already_met_completes_without_work_via_the_probe(self) -> None:
        """The adapter would refuse "already at target"; the probe answers it."""
        mission = RestMission(GOAL_ID, target_endurance=0.5)

        move = mission.next_step(world(1, stats={"endurance": 0.9}))

        assert isinstance(move, MissionProbe)
        assert move.request.idempotency_key == f"rest:{GOAL_ID}:done1"
        assert mission.any_success is False

    def test_an_adapter_refusal_rides_through_typed_and_unchanged(self) -> None:
        mission = RestMission(GOAL_ID, target_endurance=0.8)
        request = step_of(mission.next_step(world(1, stats={"endurance": 0.3})))
        mission.note_result(failed(request, seq=2, reason=ReasonCode.CAPABILITY_UNAVAILABLE))

        refused = mission.next_step(world(3, stats={"endurance": 0.3}))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert mission.ended == ENDED_REFUSED

    def test_a_target_outside_the_adapters_bounds_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="target_endurance"):
            RestMission(GOAL_ID, target_endurance=1.5)


# --------------------------------------------------------------------------
# sleep_until_rested
# --------------------------------------------------------------------------


class TestSleepUntilRested:
    def test_one_sleep_on_the_observed_bed_with_the_goals_hours(self) -> None:
        mission = SleepMission(GOAL_ID, hours=6)

        request = step_of(mission.next_step(world(1, objects=(a_bed(),))))

        assert request.action is ActionName.SURVIVAL_SLEEP
        assert request.args["hours"] == 6
        assert request.args["bed_ref"] == f"square:{DEFAULT_SESSION}:1201:3400:0"
        assert mission.phase == PHASE_SLEEP

    def test_absent_hours_stay_absent_so_the_adapter_default_applies(self) -> None:
        """The default is the adapter's; restating it here would be a second copy."""
        mission = SleepMission(GOAL_ID)

        request = step_of(mission.next_step(world(1, objects=(a_bed(),))))

        assert "hours" not in request.args
        assert request.args["bed_ref"] == f"square:{DEFAULT_SESSION}:1201:3400:0"

    def test_the_danger_refusal_surfaces_unchanged_and_is_never_retried(self) -> None:
        """The pin: POLICY_DENIED from the adapter is the goal's reason, once."""
        mission = SleepMission(GOAL_ID, hours=8)
        request = step_of(mission.next_step(world(1, objects=(a_bed(),))))
        # The engine's terminal result for a sleep the reflex guard refused —
        # the adapter's own reason code, delivered as any failure is.
        mission.note_result(failed(request, seq=2, reason=ReasonCode.POLICY_DENIED))

        refused = mission.next_step(world(3, objects=(a_bed(),)))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.POLICY_DENIED
        assert "survival.sleep refused: POLICY_DENIED" in refused.detail
        assert mission.ended == ENDED_REFUSED
        # Never retried into danger: the sealed refusal replays, and no new
        # sleep request is ever minted, however many observations arrive.
        assert mission.next_step(world(4, objects=(a_bed(),))) == refused
        assert mission.next_step(world(5, objects=(a_bed(),))) == refused

    def test_success_is_the_adapters_fatigue_fell_evidence(self) -> None:
        mission = SleepMission(GOAL_ID, hours=8)
        request = step_of(mission.next_step(world(1, objects=(a_bed(),))))
        mission.note_result(
            succeeded(
                request,
                seq=2,
                evidence={
                    "fatigue_before": 0.8,
                    "fatigue_after": 0.1,
                    "elapsed_game_seconds": 21_600.0,
                },
            )
        )

        done = mission.next_step(world(3, stats={"fatigue": 0.1}))

        assert isinstance(done, MissionComplete)
        assert mission.ended == ENDED_COMPLETE
        assert mission.any_success is True

    def test_no_observed_bed_is_a_typed_refusal(self) -> None:
        mission = SleepMission(GOAL_ID, hours=8)

        refused = mission.next_step(world(1))

        assert isinstance(refused, MissionRefused)
        assert refused.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "no bed is observed nearby" in refused.detail

    def test_the_nearest_bed_wins_deterministically(self) -> None:
        mission = SleepMission(GOAL_ID)
        far = NearbyObject(
            ref=f"object:{DEFAULT_SESSION}:7002:0",
            kind="double bed",
            distance=9.0,
            position=Position(x=1209.0, y=3400.0, z=0),
        )
        request = step_of(mission.next_step(world(1, objects=(far, a_bed()))))
        assert request.args["bed_ref"] == f"square:{DEFAULT_SESSION}:1201:3400:0"

    def test_hours_outside_the_adapters_bounds_are_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="hours"):
            SleepMission(GOAL_ID, hours=0)


# --------------------------------------------------------------------------
# the shared contract
# --------------------------------------------------------------------------


class TestTheMissionContract:
    def test_a_mission_dies_with_its_goal_and_keeps_an_earned_ending(self) -> None:
        living = TreatWoundsMission(GOAL_ID)
        living.mark_abandoned()
        assert living.ended == "cancelled"

        finished = TreatWoundsMission(GOAL_ID)
        assert isinstance(finished.next_step(world(1)), MissionProbe)
        finished.mark_abandoned()
        assert finished.ended == ENDED_COMPLETE

    def test_a_result_for_an_action_the_mission_did_not_ask_for_is_ignored(self) -> None:
        mission = RestMission(GOAL_ID, target_endurance=0.8)
        request = step_of(mission.next_step(world(1, stats={"endurance": 0.3})))
        stray = ActionResult.succeeded(
            session_id=request.session_id,
            seq=2,
            command_id="c-stray",
            action="consume.eat",
            timestamp_ms=1_700_000_000_002,
            evidence={"hunger": 0.1},
        )
        mission.note_result(stray)
        # The rest is still in flight: the stray result settled nothing.
        assert mission.next_step(world(3, stats={"endurance": 0.4})) is None

    def test_missions_refuse_an_empty_goal_id(self) -> None:
        with pytest.raises(ValueError, match="goal_id"):
            TreatWoundsMission("")
        with pytest.raises(ValueError, match="goal_id"):
            RestMission("", target_endurance=0.5)
        with pytest.raises(ValueError, match="goal_id"):
            SleepMission("")
