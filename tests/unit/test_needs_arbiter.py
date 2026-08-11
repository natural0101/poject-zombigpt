"""The needs arbiter: interrupt the current goal, satisfy the need, resume it.

:mod:`pz_agent_cli.arbiter` is the deterministic policy that decides *when*
the queue's suspension levers are pulled; these tests own its promises:

* a need **crossing** its critical line during an active mission suspends the
  goal, injects the needs goal at the front, and — after the preemptor ends,
  success or failure alike — the original resumes **mid-mission**, its drive
  and candidate list intact;
* crossings, never levels: a persistently critical need triggers once, and
  again only after satisfaction and a fresh crossing;
* bleeding outranks hunger when both cross on one observation;
* ASSISTED mode never preempts — that mode asks, it does not reshuffle;
* the queue's suspension cap is a stand-down the ledger records, never a
  fourth interruption;
* with no goal active, autonomy still injects the needs goal for itself;
* danger reaching HIGH with nothing chasing injects the retreat, while a
  chasing threat is left to the reflex guard's own band;
* the decision ledger is bounded and the suspended goal's record carries the
  ``suspended_by`` marker a status reader needs.

The harness is ``test_goal_progress.py``'s: a real queue and a real channel
behind a bound wrapper, no loop, scripted observations in, submissions
settled by hand with real engine results.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pz_agent_cli.arbiter import (
    MAX_ARBITER_LOG,
    OUTCOME_PENDING,
    TRIGGER_BLEEDING,
    TRIGGER_HUNGER,
    TRIGGER_THREAT,
)
from pz_agent_cli.navigation_planner import NavigatingPlanner
from pz_agent_cli.runtime import ActionChannel
from pz_agent_core.goals import (
    MAX_SUSPENSIONS_PER_GOAL,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    to_planner_goal,
)
from pz_agent_core.goals.model import LootScope
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ContainerKind,
    ContainerView,
    DangerLevel,
    InventoryView,
    ItemView,
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
    SessionMode,
    Wound,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    main_container_ref,
    make_container,
    make_item,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.action_doubles import FakeClock
from tests.fixtures.policy_items import food_item

ROOM = "kitchen401"
BUILDING = "apartments4"

#: Readings either side of the shipped policy's critical hunger line (0.70),
#: read off the policy here so a retuned default moves the test with it.
CALM_HUNGER = DEFAULT_POLICY_CONFIG.critical_hunger - 0.2
DIRE_HUNGER = DEFAULT_POLICY_CONFIG.critical_hunger + 0.1
FED_HUNGER = DEFAULT_POLICY_CONFIG.satisfied_hunger - 0.05


# --------------------------------------------------------------------------
# harness (test_goal_progress.py's, plus a safety block the arbiter reads)
# --------------------------------------------------------------------------


@dataclass
class Host:
    """The loop attributes the wrapper binds to, with no loop around them."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


def bound_wrapper() -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(None)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, queue, channel


def crate_ref(x: int, y: int, z: int = 0, index: int = 1) -> str:
    return f"container:{DEFAULT_SESSION}:world:{x}:{y}:{z}:{index}:0"


def crate_object(ref: str) -> NearbyObject:
    parts = ref.split(":")
    x, y, z = int(parts[3]), int(parts[4]), int(parts[5])
    return NearbyObject(
        ref=ref,
        kind="container",
        distance=max(abs(x - 1200), abs(y - 3400)),
        position=Position(x=float(x), y=float(y), z=z),
        room=ROOM,
        building=BUILDING,
    )


def observed(
    seq: int,
    *,
    hunger: float | None = CALM_HUNGER,
    mode: SessionMode = SessionMode.AUTONOMOUS,
    danger: DangerLevel = DangerLevel.NONE,
    bleeding: bool = False,
    zombies: tuple[NearbyZombie, ...] = (),
    objects: tuple[NearbyObject, ...] = (),
    containers: tuple[ContainerView, ...] = (),
    items: tuple[ItemView, ...] = (),
) -> Observation:
    main = make_container(
        main_container_ref(), ContainerKind.PLAYER_MAIN, capacity=20.0, used_capacity=3.0
    )
    wounds = (
        [Wound(ref=f"wound:{DEFAULT_SESSION}:Head", kind="scratch", severity=0.5, bleeding=True)]
        if bleeding
        else []
    )
    # The fixture merges into a stat-rich default, so "unreported" has to be
    # said explicitly: a null reading, the shape a build without the reader
    # actually sends, never a silently inherited number.
    stats: dict[str, Any] = {"hunger": hunger}
    return make_observation(
        seq=seq,
        player=make_player(
            position=Position(x=1200.0, y=3400.0, z=0, direction="S"),
            room=ROOM,
            building=BUILDING,
            stats=stats,
            wounds=wounds,
        ),
        safety=make_safety(mode=mode, danger_level=danger),
        nearby=NearbyView(objects=list(objects), zombies=list(zombies)),
        inventory=InventoryView(containers=[main, *containers], items=list(items)),
    )


def active_goal(queue: GoalQueue, request: GoalRequest) -> GoalRecord:
    admission = queue.submit(request)
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def loot_request(key: str = "loot-key") -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.LOOT_AREA,
        idempotency_key=key,
        params=GoalParams(scope=LootScope.ROOM),
    )


def take_request(channel: ActionChannel) -> tuple[str, Any]:
    taken = channel.take_next()
    assert taken is not None, "expected a channel submission"
    return taken


def settle_success(channel: ActionChannel, *, seq: int, evidence: dict[str, object]) -> None:
    action_id, request = take_request(channel)
    channel.settle(
        action_id,
        ActionResult.succeeded(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            evidence=evidence,
        ),
    )


# --------------------------------------------------------------------------
# the founding scenario: interrupt a loot sweep, eat, resume mid-list
# --------------------------------------------------------------------------


class TestInterruptSatisfyResume:
    def loot_world(
        self, seq: int, *, hunger: float, crates: tuple[str, ...], items: tuple[ItemView, ...] = ()
    ) -> Observation:
        return observed(
            seq,
            hunger=hunger,
            objects=tuple(crate_object(ref) for ref in crates),
            containers=tuple(
                make_container(ref, ContainerKind.WORLD, name="crate") for ref in crates
            ),
            items=items,
        )

    def drive_first_crate(
        self,
        wrapper: NavigatingPlanner,
        queue: GoalQueue,
        channel: ActionChannel,
        *,
        crates: tuple[str, ...],
        stock: tuple[ItemView, ...],
    ) -> GoalRecord:
        """Serve a loot goal through crate one's whole open/inspect/transfer."""
        record = active_goal(queue, loot_request())
        goal = to_planner_goal(record)
        first = crates[0]

        calm_1 = self.loot_world(1, hunger=CALM_HUNGER, crates=crates, items=stock)
        assert wrapper.propose_for_goal(goal, calm_1) is None
        settle_success(channel, seq=1, evidence={"container_ref": first})
        calm_2 = self.loot_world(2, hunger=CALM_HUNGER, crates=crates, items=stock)
        assert wrapper.propose_for_goal(goal, calm_2) is None
        settle_success(channel, seq=2, evidence={"container_ref": first, "item_count": 1})
        calm_3 = self.loot_world(3, hunger=CALM_HUNGER, crates=crates, items=stock)
        assert wrapper.propose_for_goal(goal, calm_3) is None
        settle_success(
            channel,
            seq=3,
            evidence={"destination_ref": main_container_ref(), "transferred": [stock[0].ref]},
        )
        # The transfer's settled result has not been folded back yet — the
        # mission reads it on its next serve, which is exactly the position
        # a preemption interrupts: mid-candidate, result in hand.
        return record

    def test_a_hunger_crossing_suspends_the_loot_goal_and_injects_the_meal(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        crates = (crate_ref(1205, 3400, index=1), crate_ref(1206, 3400, index=2))
        stock = (
            make_item(f"item:{DEFAULT_SESSION}:world:1205:3400:0:1:0:beans:0", crates[0]),
            make_item(f"item:{DEFAULT_SESSION}:world:1206:3400:0:2:0:soup:0", crates[1]),
        )
        record = self.drive_first_crate(wrapper, queue, channel, crates=crates, stock=stock)
        goal = to_planner_goal(record)

        # The crossing tick: hunger rises through the critical line while the
        # loot goal is being served. The arbiter suspends it and injects the
        # meal before the mission can start crate two.
        assert (
            wrapper.propose_for_goal(goal, self.loot_world(4, hunger=DIRE_HUNGER, crates=crates))
            is None
        )

        assert queue.active is None, "the loot goal was suspended, not served"
        parked = queue.record(record.goal_id)
        assert parked is not None
        assert parked.state is GoalState.PENDING
        assert parked.suspended_by == f"arb.{record.goal_id}.{TRIGGER_HUNGER}.1"
        assert "hunger" in parked.detail
        front = queue.pending[0]
        assert front.kind is GoalKind.SATISFY_HUNGER, "the meal runs next"
        assert queue.pending[1].goal_id == record.goal_id, "the loot goal resumes right after"
        assert channel.pending_count == 0, "no step was submitted for suspended work"
        assert wrapper.tracked_missions == 1, "the mission drive survives the suspension"
        entry = wrapper.arbiter_log[-1]
        assert entry["trigger"] == TRIGGER_HUNGER
        assert entry["suspended_goal"] == record.goal_id
        assert entry["injected_goal"] == front.goal_id
        assert entry["outcome"] == OUTCOME_PENDING

    def test_the_loot_mission_resumes_mid_candidate_list_after_the_meal(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        crates = (crate_ref(1205, 3400, index=1), crate_ref(1206, 3400, index=2))
        stock = (
            make_item(f"item:{DEFAULT_SESSION}:world:1205:3400:0:1:0:beans:0", crates[0]),
            make_item(f"item:{DEFAULT_SESSION}:world:1206:3400:0:2:0:soup:0", crates[1]),
        )
        record = self.drive_first_crate(wrapper, queue, channel, crates=crates, stock=stock)
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(record), self.loot_world(4, hunger=DIRE_HUNGER, crates=crates)
            )
            is None
        )

        # The meal is served: carried safe food, eaten, verified on the moved
        # stat — the consume mission's own success path, driven by hand.
        meal = queue.activate_next().goal
        assert meal is not None and meal.kind is GoalKind.SATISFY_HUNGER
        larder = (food_item("beans"),)
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(meal), observed(5, hunger=DIRE_HUNGER, items=larder)
            )
            is None
        )
        settle_success(
            channel,
            seq=5,
            evidence={"hunger_before": DIRE_HUNGER, "hunger_after": FED_HUNGER},
        )
        assert (
            wrapper.propose_for_goal(to_planner_goal(meal), observed(6, hunger=FED_HUNGER)) is None
        )
        fed = queue.record(meal.goal_id)
        assert fed is not None and fed.state is GoalState.SUCCEEDED

        # Resumption is ordinary activation: the parked goal comes back with
        # its marker consumed, and the *same* mission continues — crate one
        # stays inspected, the next submission opens crate two.
        resumed = queue.activate_next().goal
        assert resumed is not None and resumed.goal_id == record.goal_id
        assert resumed.suspended_by is None
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(resumed), self.loot_world(7, hunger=FED_HUNGER, crates=crates)
            )
            is None
        )
        _, request = take_request(channel)
        assert request.action is ActionName.CONTAINER_OPEN_NEARBY
        assert request.args["container_ref"] == crates[1], "the sweep continues, not restarts"
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["containers_inspected"] == [crates[0]], "crate one was not swept again"
        entry = wrapper.arbiter_log[-1]
        assert entry["outcome"] == GoalState.SUCCEEDED.value, "the ledger settled on the terminal"

    def test_a_failed_meal_still_resumes_the_original_and_the_ledger_says_failed(self) -> None:
        """No food anywhere: the preemptor fails and orphans nothing."""
        wrapper, queue, channel = bound_wrapper()
        crates = (crate_ref(1205, 3400, index=1), crate_ref(1206, 3400, index=2))
        stock = (
            make_item(f"item:{DEFAULT_SESSION}:world:1205:3400:0:1:0:beans:0", crates[0]),
            make_item(f"item:{DEFAULT_SESSION}:world:1206:3400:0:2:0:soup:0", crates[1]),
        )
        record = self.drive_first_crate(wrapper, queue, channel, crates=crates, stock=stock)
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(record), self.loot_world(4, hunger=DIRE_HUNGER, crates=crates)
            )
            is None
        )

        meal = queue.activate_next().goal
        assert meal is not None and meal.kind is GoalKind.SATISFY_HUNGER
        # A bare world: nothing carried, nothing known, nothing observed to
        # fetch from. The consume mission's typed end, not a hang.
        bare_world = observed(5, hunger=DIRE_HUNGER)
        assert wrapper.propose_for_goal(to_planner_goal(meal), bare_world) is None
        starved = queue.record(meal.goal_id)
        assert starved is not None
        assert starved.state is GoalState.FAILED
        assert starved.reason_code is ReasonCode.NO_SAFE_FOOD

        resumed = queue.activate_next().goal
        assert resumed is not None and resumed.goal_id == record.goal_id
        assert wrapper.tracked_missions == 1, "the drive waited out the failed preemptor"
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(resumed), self.loot_world(6, hunger=DIRE_HUNGER, crates=crates)
            )
            is None
        )
        _, request = take_request(channel)
        assert request.args["container_ref"] == crates[1]
        entry = wrapper.arbiter_log[-1]
        assert entry["outcome"] == GoalState.FAILED.value


# --------------------------------------------------------------------------
# the priority table and the mode gate
# --------------------------------------------------------------------------


class TestTriggersAndGates:
    def test_bleeding_outranks_hunger_when_both_cross_at_once(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = active_goal(queue, loot_request())
        goal = to_planner_goal(record)
        # A crateless sweep answers its completion probe; the tick's answer
        # is not this test's subject — the arbitration on tick two is.
        wrapper.propose_for_goal(goal, observed(1, hunger=CALM_HUNGER))

        assert (
            wrapper.propose_for_goal(goal, observed(2, hunger=DIRE_HUNGER, bleeding=True)) is None
        )

        front = queue.pending[0]
        assert front.kind is GoalKind.TREAT_WOUNDS, "blood first; the meal waits"
        assert wrapper.arbiter_log[-1]["trigger"] == TRIGGER_BLEEDING

    def test_assisted_mode_never_preempts(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = active_goal(queue, loot_request())
        goal = to_planner_goal(record)
        wrapper.propose_for_goal(goal, observed(1, hunger=CALM_HUNGER, mode=SessionMode.ASSISTED))

        wrapper.propose_for_goal(goal, observed(2, hunger=DIRE_HUNGER, mode=SessionMode.ASSISTED))

        active = queue.active
        assert active is not None and active.goal_id == record.goal_id, "nothing was reshuffled"
        assert queue.pending == ()
        assert wrapper.arbiter_log == ()

    def test_a_level_is_not_an_edge_and_a_fresh_crossing_is(self) -> None:
        """Idle autonomy: one injection per crossing, held levels are silent."""
        wrapper, queue, channel = bound_wrapper()
        assert wrapper.propose(observed(1, hunger=CALM_HUNGER)) is None
        assert wrapper.propose(observed(2, hunger=DIRE_HUNGER)) is None
        assert len(queue.pending) == 1, "the crossing injected the meal for idle autonomy"
        first_meal = queue.pending[0]
        assert first_meal.kind is GoalKind.SATISFY_HUNGER
        assert (
            first_meal.key_digest
            == GoalRequest(
                kind=GoalKind.SATISFY_HUNGER, idempotency_key=f"arb.idle.{TRIGGER_HUNGER}.1"
            ).digest
        ), "the idle injection's key is deterministic"

        # The level holds; nothing new is injected however often it is seen.
        assert wrapper.propose(observed(3, hunger=DIRE_HUNGER)) is None
        assert wrapper.propose(observed(4, hunger=DIRE_HUNGER)) is None
        assert len(queue.pending) == 1
        assert len(wrapper.arbiter_log) == 1

        # The meal is served and the need satisfied...
        meal = queue.activate_next().goal
        assert meal is not None
        assert (
            wrapper.propose_for_goal(
                to_planner_goal(meal), observed(5, hunger=DIRE_HUNGER, items=(food_item("beans"),))
            )
            is None
        )
        settle_success(
            channel, seq=5, evidence={"hunger_before": DIRE_HUNGER, "hunger_after": FED_HUNGER}
        )
        assert (
            wrapper.propose_for_goal(to_planner_goal(meal), observed(6, hunger=FED_HUNGER)) is None
        )
        fed = queue.record(meal.goal_id)
        assert fed is not None and fed.state is GoalState.SUCCEEDED

        # ...and only a fresh crossing triggers again. The backlog is bound
        # to locals per read: the property answers fresh state every time,
        # which a narrowed member expression would misrepresent.
        assert wrapper.propose(observed(7, hunger=CALM_HUNGER)) is None
        emptied: tuple[GoalRecord, ...] = queue.pending
        assert emptied == ()
        assert wrapper.propose(observed(8, hunger=DIRE_HUNGER)) is None
        refilled: tuple[GoalRecord, ...] = queue.pending
        assert len(refilled) == 1
        assert len(wrapper.arbiter_log) == 2

    def test_danger_reaching_high_with_nothing_chasing_injects_the_retreat(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        assert wrapper.propose(observed(1)) is None

        # The mod's own word reaches HIGH with no zombie observed chasing —
        # the picture the reflex guard's rungs do not drive anywhere.
        assert wrapper.propose(observed(2, danger=DangerLevel.HIGH)) is None

        assert len(queue.pending) == 1
        assert queue.pending[0].kind is GoalKind.AVOID_THREAT
        assert wrapper.arbiter_log[-1]["trigger"] == TRIGGER_THREAT

    def test_a_chasing_threat_is_the_reflex_guards_band_not_a_trigger(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        assert wrapper.propose(observed(1)) is None

        chaser = NearbyZombie(
            ref=f"zombie:{DEFAULT_SESSION}:z1",
            distance=5.0,
            visible=True,
            chasing=True,
            position=Position(x=1205.0, y=3400.0, z=0),
        )
        assert wrapper.propose(observed(2, zombies=(chaser,))) is None

        assert queue.pending == (), "a chase belongs to the guard's stop, not to a retreat goal"
        assert wrapper.arbiter_log == ()


# --------------------------------------------------------------------------
# bounds and refusals
# --------------------------------------------------------------------------


class TestBoundsAndRefusals:
    def exhaust_suspensions(self, queue: GoalQueue, record: GoalRecord) -> None:
        """Spend the goal's whole suspension budget through the queue's own levers."""
        for round_ in range(MAX_SUSPENSIONS_PER_GOAL):
            parked = queue.suspend(
                record.goal_id, by_goal_id=f"earlier-{round_}", reason="earlier wave", now_ms=0
            )
            assert parked.goal is not None, parked.refusal
            resumed = queue.activate_next()
            assert resumed.goal is not None, resumed.refusal

    def test_the_fourth_suspension_is_a_stand_down_the_ledger_records(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = active_goal(queue, loot_request())
        self.exhaust_suspensions(queue, record)
        goal = to_planner_goal(record)
        wrapper.propose_for_goal(goal, observed(1, hunger=CALM_HUNGER))

        wrapper.propose_for_goal(goal, observed(2, hunger=DIRE_HUNGER))

        active = queue.active
        assert active is not None and active.goal_id == record.goal_id, "the goal runs on"
        assert queue.pending == (), "no meal was injected over a refused suspension"
        entry = wrapper.arbiter_log[-1]
        assert entry["trigger"] == TRIGGER_HUNGER
        assert entry["injected_goal"] is None
        assert entry["outcome"] == f"stood_down:{ReasonCode.QUEUE_REJECTED.value}"

    def test_the_ledger_is_bounded_at_its_cap(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = active_goal(
            queue, GoalRequest(kind=GoalKind.READ_FOR_BOREDOM, idempotency_key="read-key")
        )
        self.exhaust_suspensions(queue, record)
        goal = to_planner_goal(record)

        # Every rise through the line is one recorded stand-down; the falls
        # trigger nothing. Twenty crossings, sixteen kept.
        seq = 0
        for _ in range(MAX_ARBITER_LOG + 4):
            seq += 1
            wrapper.propose_for_goal(goal, observed(seq, hunger=CALM_HUNGER))
            seq += 1
            wrapper.propose_for_goal(goal, observed(seq, hunger=DIRE_HUNGER))

        assert len(wrapper.arbiter_log) == MAX_ARBITER_LOG

    def test_an_unreported_stat_never_crosses(self) -> None:
        """No reading, no edge: a build that reports nothing triggers nothing."""
        wrapper, queue, _ = bound_wrapper()
        assert wrapper.propose(observed(1, hunger=None)) is None
        assert wrapper.propose(observed(2, hunger=None)) is None
        assert wrapper.propose(observed(3, hunger=DIRE_HUNGER)) is None, (
            "the first real reading is one reading, not a crossing"
        )

        assert queue.pending == ()
        assert wrapper.arbiter_log == ()
