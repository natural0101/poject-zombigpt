"""The consume mission behind the wrapper: the mandatory chain with no planner.

The selection policies — which tin is safe and which bottle wins — are
``test_policy_food.py``'s and ``test_policy_drink.py``'s subject, journeys are
``test_navigation_executor.py``'s, and the loop-side wiring is
``test_cli_goal_wiring.py``'s. These tests own the mission's own promises,
driven the way ``test_loot_mission.py`` drives loot sweeps: a real
:class:`~pz_agent_core.goals.GoalQueue` and a real
:class:`~pz_agent_cli.runtime.ActionChannel` behind a bound
:class:`~pz_agent_cli.navigation_planner.NavigatingPlanner`, scripted
observations in, channel submissions settled by hand with real
:class:`~pz_agent_core.protocol.ActionResult` values, and every assertion on
the queue's, the channel's or the report's own answers:

* a need already at its target completes without work through the goal-seam
  probe, and an absent stat is the typed refusal — absent is never zero;
* carried safe food is eaten and the goal succeeds only after the fresh
  observation shows the stat moved;
* the user's reserve outranks hunger: reserved-only food is never eaten, at
  any hunger, and the mission fails typed instead;
* a typed consume refusal spends that candidate and the next one is tried;
* the fetch chain runs approach-free open → inspect → transfer → eat over a
  scripted world container and succeeds on the moved stat;
* exhausted candidates are the typed ``NO_SAFE_FOOD`` failure carrying the
  skip record; phases surface through ``goal_progress``; the mission dies
  with its cancelled goal; the wrapped planner is never asked; and the same
  script replayed is the same requests, byte for byte.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pz_agent_cli.consume_mission import (
    CONSUME_PHASES,
    ENDED_NOTHING_FOUND,
    ENDED_UNREPORTED,
    HUNGER,
    MAX_CANDIDATES_PER_MISSION,
    MAX_FETCH_DISTANCE,
    PHASE_CHECK,
    PHASE_CONSUME,
    PHASE_FETCH,
    PHASE_VERIFY,
    ConsumeMission,
    ConsumeMissionLimits,
)
from pz_agent_cli.loot_mission import ENDED_CANCELLED, ENDED_COMPLETE
from pz_agent_cli.navigation_planner import (
    _MAX_KNOWN_CONTAINERS,
    MAX_TRACKED_CONSUMES,
    NavigatingPlanner,
)
from pz_agent_cli.runtime import ActionChannel
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals import (
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    to_planner_goal,
)
from pz_agent_core.memory import CEILINGS
from pz_agent_core.navigation import LocalMap
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ContainerKind,
    ContainerView,
    InventoryView,
    ItemView,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    main_container_ref,
    make_container,
    make_observation,
    make_player,
)
from tests.fixtures.action_doubles import FakeClock
from tests.fixtures.policy_items import drink_item, food_item

# --------------------------------------------------------------------------
# the world under the mission
# --------------------------------------------------------------------------


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
        room="kitchen401",
        building="apartments4",
    )


def described_crate(ref: str, **overrides: Any) -> ContainerView:
    return make_container(ref, ContainerKind.WORLD, name="crate", **overrides)


def observed(
    seq: int,
    *,
    stats: dict[str, Any] | None = None,
    items: tuple[ItemView, ...] = (),
    containers: tuple[ContainerView, ...] = (),
    objects: tuple[NearbyObject, ...] = (),
    inventory_absent: bool = False,
) -> Observation:
    main = make_container(
        main_container_ref(), ContainerKind.PLAYER_MAIN, capacity=20.0, used_capacity=3.0
    )
    inventory = (
        None
        if inventory_absent
        else InventoryView(containers=[main, *containers], items=list(items))
    )
    return make_observation(
        seq=seq,
        player=make_player(
            position=Position(x=1200.0, y=3400.0, z=0, direction="S"),
            stats=stats if stats is not None else {"hunger": 0.6},
        ),
        nearby=NearbyView(objects=list(objects)),
        inventory=inventory,
    )


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


@dataclass
class Host:
    """The loop attributes the wrapper binds to, with no loop around them."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


class SpyPlanner:
    """A goal-capable planner that records every ask and answers nothing."""

    def __init__(self) -> None:
        self.propose_calls = 0
        self.goal_calls: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.propose_calls += 1
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goal_calls.append(goal)
        return None


@dataclass(frozen=True)
class KnownSquare:
    x: int
    y: int
    z: int = 0


@dataclass(frozen=True)
class KnownRecord:
    """A remembered container, as much of one as the wrapper's port reads."""

    tail: str
    square: KnownSquare | None
    categories: tuple[str, ...] = ()
    item_count: int = -1


@dataclass
class FakeMemory:
    """A memory answering the reserve and container questions, nothing else."""

    reserved: frozenset[str] = frozenset()
    known: tuple[KnownRecord, ...] = ()

    def reserves_item(self, full_type: str, /) -> bool:
        return full_type in self.reserved

    def containers(self) -> tuple[KnownRecord, ...]:
        return self.known


def bound_wrapper(
    inner: SpyPlanner | None = None,
    *,
    limits: ConsumeMissionLimits | None = None,
    memory: object | None = None,
) -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(inner, consume_limits=limits, loot_memory=memory)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, queue, channel


def consume_goal(
    queue: GoalQueue,
    *,
    kind: GoalKind = GoalKind.SATISFY_HUNGER,
    satisfy_to: float | None = None,
    key: str = "eat-key",
) -> GoalRecord:
    admission = queue.submit(
        GoalRequest(kind=kind, idempotency_key=key, params=GoalParams(satisfy_to=satisfy_to))
    )
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def take_request(channel: ActionChannel) -> tuple[str, ActionRequest]:
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


def settle_failure(channel: ActionChannel, *, seq: int, reason_code: ReasonCode) -> None:
    action_id, request = take_request(channel)
    channel.settle(
        action_id,
        ActionResult.failure(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            reason_code=reason_code,
        ),
    )


def take_and_settle(channel: ActionChannel, *, seq: int) -> ActionRequest:
    """Take the waiting submission, settle it succeeded, hand back the request."""
    action_id, request = take_request(channel)
    channel.settle(
        action_id,
        ActionResult.succeeded(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            evidence={"observed": True},
        ),
    )
    return request


# --------------------------------------------------------------------------
# the need check
# --------------------------------------------------------------------------


class TestTheNeedCheck:
    def test_a_need_already_at_target_completes_through_the_probe(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, stats={"hunger": 0.1})
        )

        # No work to do and no result to succeed with: the one goal-seam
        # request, whose observed success the loop settles the goal on.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.idempotency_key.startswith(f"consume:{record.goal_id}")
        assert channel.pending_count == 0, "nothing travelled the action channel"
        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_COMPLETE
        assert report["consumed"] == []

    def test_the_satisfy_to_parameter_is_honoured_over_the_policy_default(self) -> None:
        """hunger 0.3 satisfies satisfy_to=0.4 although the policy aims at 0.15."""
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue, satisfy_to=0.4)

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, stats={"hunger": 0.3})
        )

        assert value is not None and value.action is ActionName.MOVEMENT_MOVE_TO
        assert channel.pending_count == 0

    def test_an_absent_stat_is_the_typed_refusal_never_zero(self) -> None:
        """A null hunger reading is "not reported", which is never "satisfied"."""
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, stats={"hunger": None})
        )

        assert value is None
        assert channel.pending_count == 0
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "does not report hunger" in ended.detail
        report = wrapper.consume_report(record.goal_id)
        assert report is not None and report["ended"] == ENDED_UNREPORTED


# --------------------------------------------------------------------------
# carried first
# --------------------------------------------------------------------------


class TestCarriedFood:
    def test_carried_safe_food_is_eaten_and_the_goal_succeeds_on_the_moved_stat(
        self,
    ) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        beans = food_item("beans")

        assert wrapper.propose_for_goal(goal, observed(1, items=(beans,))) is None
        action_id, request = take_request(channel)
        assert request.action is ActionName.CONSUME_EAT
        assert request.args["item_ref"] == beans.ref
        assert request.args["fraction"] == 1.0, "no verified portioning probe: a whole unit"
        assert request.idempotency_key.startswith(f"consume:{record.goal_id}")
        channel.settle(
            action_id,
            ActionResult.succeeded(
                session_id=request.session_id,
                seq=1,
                command_id=action_id,
                action=request.action.value,
                timestamp_ms=1_700_000_000_001,
                evidence={"hunger_before": 0.6, "hunger_after": 0.1},
            ),
        )

        # The goal is not settled on the adapter's word alone: the fresh
        # observation shows the need at 0.1, and only then does it succeed.
        assert wrapper.propose_for_goal(goal, observed(2, stats={"hunger": 0.1})) is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert finished.reason_code is ReasonCode.POSTCONDITION_MET
        assert finished.evidence_keys, "success carries the observed evidence keys"
        assert wrapper.tracked_consumes == 0, "the mission dies with its finished goal"

        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_COMPLETE
        assert report["need"] == "hunger"
        assert report["stat_before"] == 0.6
        assert report["stat_after"] == 0.1
        assert report["consumed"] == [{"item": "Tinned Beans", "fraction": 1.0}]
        assert report["candidates_tried"] == 1

    def test_a_backpack_item_is_brought_to_hand_first(self) -> None:
        """§4.7 honoured as its own observable step, never folded into the eat."""
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        backpack = make_container(backpack_container_ref(), ContainerKind.WORN, name="Backpack")
        beans = food_item("beans", container_ref=backpack_container_ref())

        assert (
            wrapper.propose_for_goal(goal, observed(1, items=(beans,), containers=(backpack,)))
            is None
        )
        moved = take_and_settle(channel, seq=1)
        assert moved.action is ActionName.INVENTORY_ENSURE_MAIN
        assert moved.args == {"item_ref": beans.ref}

        in_main = food_item("beans")
        assert wrapper.propose_for_goal(goal, observed(2, items=(in_main,))) is None
        _, request = take_request(channel)
        assert request.action is ActionName.CONSUME_EAT
        assert request.args["item_ref"] == in_main.ref

    def test_the_reserve_outranks_hunger_even_critical_hunger(self) -> None:
        """The agent starves before eating the user's strategic reserve.

        Hunger 0.9 is past the critical threshold that opens the *policy's*
        strategic-reserve stock; the memory reserve has no such override, so
        the only carried food stays on the character and the goal fails typed.
        """
        memory = FakeMemory(reserved=frozenset({"Base.TinnedBeans"}))
        wrapper, queue, channel = bound_wrapper(memory=memory)
        record = consume_goal(queue)
        beans = food_item("beans")

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, stats={"hunger": 0.9}, items=(beans,))
        )

        assert value is None
        assert channel.pending_count == 0, "the reserved tin must never reach the mouth"
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_SAFE_FOOD
        assert "no safe FOOD was found in 0 reachable containers" in ended.detail
        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_NOTHING_FOUND
        assert report["reserved_withheld"] == 1
        assert report["consumed"] == []

    def test_a_typed_refusal_spends_the_candidate_and_the_next_is_tried(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        # The policy ranks the calorie-denser soup first, deterministically.
        soup = food_item("soup", full_type="Base.Soup", display_name="Soup", calories=800.0)
        beans = food_item("beans", calories=400.0)

        assert wrapper.propose_for_goal(goal, observed(1, items=(soup, beans))) is None
        first_id, first = take_request(channel)
        assert first.action is ActionName.CONSUME_EAT
        assert first.args["item_ref"] == soup.ref
        # The taken submission's terminal answer: a typed safety ack.
        channel.settle(
            first_id,
            ActionResult.failure(
                session_id=first.session_id,
                seq=1,
                command_id=first_id,
                action=first.action.value,
                timestamp_ms=1_700_000_000_001,
                reason_code=ReasonCode.NO_SAFE_FOOD,
            ),
        )

        assert wrapper.propose_for_goal(goal, observed(2, items=(soup, beans))) is None
        _second_id, second = take_request(channel)
        assert second.action is ActionName.CONSUME_EAT
        assert second.args["item_ref"] == beans.ref, "the refused soup is spent, not retried"

        served = queue.record(record.goal_id)
        assert served is not None and served.state is GoalState.ACTIVE
        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["skipped"] == [
            {"ref": soup.ref, "reason": "consume.eat refused: NO_SAFE_FOOD"}
        ]


# --------------------------------------------------------------------------
# the fetch chain
# --------------------------------------------------------------------------


class TestTheFetchChain:
    def test_open_inspect_transfer_eat_end_to_end_over_a_world_container(self) -> None:
        spy = SpyPlanner()
        wrapper, queue, channel = bound_wrapper(spy)
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)
        beans = food_item("beans", container_ref=crate)

        def hungry(seq: int, *, items: tuple[ItemView, ...]) -> Observation:
            return observed(
                seq,
                items=items,
                containers=(described_crate(crate),),
                objects=(crate_object(crate),),
            )

        # Nothing carried: the mission goes to the shelf it can see.
        assert wrapper.propose_for_goal(goal, hungry(1, items=(beans,))) is None
        opened = take_and_settle(channel, seq=1)
        assert opened.action is ActionName.CONTAINER_OPEN_NEARBY
        assert opened.args == {"container_ref": crate}

        assert wrapper.propose_for_goal(goal, hungry(2, items=(beans,))) is None
        inspected = take_and_settle(channel, seq=2)
        assert inspected.action is ActionName.CONTAINER_INSPECT
        assert inspected.args == {"container_ref": crate}

        assert wrapper.propose_for_goal(goal, hungry(3, items=(beans,))) is None
        transferred = take_and_settle(channel, seq=3)
        assert transferred.action is ActionName.INVENTORY_TRANSFER
        assert transferred.args == {
            "item_ref": beans.ref,
            "destination_container_ref": main_container_ref(),
        }

        # The tin observed in the main inventory: the carried path eats it.
        in_main = food_item("beans")
        assert wrapper.propose_for_goal(goal, hungry(4, items=(in_main,))) is None
        eaten_id, eaten = take_request(channel)
        assert eaten.action is ActionName.CONSUME_EAT
        assert eaten.args["item_ref"] == in_main.ref
        channel.settle(
            eaten_id,
            ActionResult.succeeded(
                session_id=eaten.session_id,
                seq=4,
                command_id=eaten_id,
                action=eaten.action.value,
                timestamp_ms=1_700_000_000_004,
                evidence={"hunger_before": 0.6, "hunger_after": 0.1},
            ),
        )

        assert wrapper.propose_for_goal(goal, observed(5, stats={"hunger": 0.1})) is None
        finished = queue.record(record.goal_id)
        assert finished is not None and finished.state is GoalState.SUCCEEDED
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "satisfy_hunger must never reach a plan provider"
        )
        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_COMPLETE
        assert report["containers_visited"] == [crate]
        assert report["consumed"] == [{"item": "Tinned Beans", "fraction": 1.0}]

    def test_a_container_with_nothing_safe_is_a_recorded_skip_and_the_typed_end(
        self,
    ) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)
        rotten = food_item("old", container_ref=crate, freshness="rotten")

        def world(seq: int) -> Observation:
            return observed(
                seq,
                items=(rotten,),
                containers=(described_crate(crate),),
                objects=(crate_object(crate),),
            )

        assert wrapper.propose_for_goal(goal, world(1)) is None
        assert take_and_settle(channel, seq=1).action is ActionName.CONTAINER_OPEN_NEARBY
        assert wrapper.propose_for_goal(goal, world(2)) is None
        assert take_and_settle(channel, seq=2).action is ActionName.CONTAINER_INSPECT

        # The safety gate is the policy's: the rotten tin is never transferred,
        # the shelf becomes a recorded skip, and nothing else is reachable.
        assert wrapper.propose_for_goal(goal, world(3)) is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_SAFE_FOOD
        assert "no safe FOOD was found in 1 reachable containers" in ended.detail
        report = wrapper.consume_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_NOTHING_FOUND
        assert report["skipped"] == [{"ref": crate, "reason": "no safe FOOD inside"}]

    def test_a_remembered_food_shelf_outranks_an_uninspected_one(self) -> None:
        """Tier order: memory's FOOD categories beat a nearer unknown shelf."""
        pantry = crate_ref(1204, 3400)
        unknown = crate_ref(1201, 3400, index=2)
        memory = FakeMemory(
            known=(
                KnownRecord(
                    tail="world:1204:3400:0:1:0",
                    square=KnownSquare(1204, 3400),
                    categories=("FOOD",),
                    item_count=3,
                ),
            )
        )
        wrapper, queue, channel = bound_wrapper(memory=memory)
        record = consume_goal(queue)

        assert (
            wrapper.propose_for_goal(
                to_planner_goal(record),
                observed(
                    1,
                    containers=(described_crate(pantry), described_crate(unknown)),
                    objects=(crate_object(pantry), crate_object(unknown)),
                ),
            )
            is None
        )

        _, request = take_request(channel)
        assert request.action is ActionName.CONTAINER_OPEN_NEARBY
        assert request.args == {"container_ref": pantry}, (
            "the shelf memory filed under FOOD wins over the nearer unknown"
        )


# --------------------------------------------------------------------------
# progress, lifetimes, determinism
# --------------------------------------------------------------------------


class TestPhasesAndLifetimes:
    def test_the_phase_vocabulary_is_the_stated_closed_set(self) -> None:
        assert CONSUME_PHASES == ("check", "fetch", "consume", "verify")

    def test_the_phases_surface_through_goal_progress(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)
        beans = food_item("beans", container_ref=crate)

        def world(seq: int, *, items: tuple[ItemView, ...] = (beans,)) -> Observation:
            return observed(
                seq,
                items=items,
                containers=(described_crate(crate),),
                objects=(crate_object(crate),),
            )

        # An observation with no inventory tier: the mission waits in check.
        assert (
            wrapper.propose_for_goal(goal, observed(1, objects=(), inventory_absent=True)) is None
        )
        progress = wrapper.goal_progress(record.goal_id)
        assert progress is not None and progress.phase == PHASE_CHECK

        assert wrapper.propose_for_goal(goal, world(2)) is None
        progress = wrapper.goal_progress(record.goal_id)
        assert progress is not None and progress.phase == PHASE_FETCH
        take_and_settle(channel, seq=2)  # open
        assert wrapper.propose_for_goal(goal, world(3)) is None
        take_and_settle(channel, seq=3)  # inspect
        assert wrapper.propose_for_goal(goal, world(4)) is None
        take_and_settle(channel, seq=4)  # transfer

        in_main = food_item("beans")
        assert wrapper.propose_for_goal(goal, world(5, items=(in_main,))) is None
        progress = wrapper.goal_progress(record.goal_id)
        assert progress is not None and progress.phase == PHASE_CONSUME
        assert progress.counters["candidates_tried"] == 2

    def test_verify_is_the_phase_between_the_blessed_bite_and_the_fresh_reading(
        self,
    ) -> None:
        """The mission's own surface, because the wrapper folds and decides in
        one call: after the consume's terminal result lands and before the
        next observation is read, the mission stands in ``verify``."""
        mission = ConsumeMission(
            "goal-1",
            need=HUNGER,
            local_map=LocalMap(),
            policy=DEFAULT_POLICY_CONFIG,
            is_reserved=lambda full_type: False,
            known_containers=lambda: (),
        )
        beans = food_item("beans")
        move = mission.next_step(observed(1, items=(beans,)))
        assert move is not None
        request = getattr(move, "request", None)
        assert request is not None and request.action is ActionName.CONSUME_EAT
        assert mission.phase == PHASE_CONSUME

        mission.note_result(
            ActionResult.succeeded(
                session_id=DEFAULT_SESSION,
                seq=2,
                command_id="cmd-1",
                action="consume.eat",
                timestamp_ms=1_700_000_000_002,
                evidence={"hunger_before": 0.6, "hunger_after": 0.1},
            )
        )
        assert mission.phase == PHASE_VERIFY

    def test_the_mission_dies_with_its_cancelled_goal_and_the_report_survives(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)

        assert (
            wrapper.propose_for_goal(
                goal,
                observed(1, containers=(described_crate(crate),), objects=(crate_object(crate),)),
            )
            is None
        )
        assert wrapper.tracked_consumes == 1
        assert channel.pending_count == 1

        assert queue.request_cancel(record.goal_id) is True
        queue.tick()
        # The next call — any call — prunes; nothing new is submitted.
        assert wrapper.propose(observed(2)) is None

        assert wrapper.tracked_consumes == 0, "the mission must die with its goal"
        assert channel.pending_count == 1, "the admitted step stays the engine's to finish"
        cancelled = queue.record(record.goal_id)
        assert cancelled is not None and cancelled.state is GoalState.CANCELLED
        report = wrapper.consume_report(record.goal_id)
        assert report is not None, "the report must survive the goal"
        assert report["ended"] == ENDED_CANCELLED

    def test_a_planner_less_wrapper_serves_both_kinds(self) -> None:
        wrapper, queue, channel = bound_wrapper(None)
        hunger = consume_goal(queue, key="eat-key")
        beans = food_item("beans")
        assert (
            wrapper.propose_for_goal(to_planner_goal(hunger), observed(1, items=(beans,))) is None
        )
        _, eat = take_request(channel)
        assert eat.action is ActionName.CONSUME_EAT

        # Thirst 0.8 is critical, so the policy's last-container rule does not
        # withhold the only bottle when partial drinking is unverified.
        wrapper2, queue2, channel2 = bound_wrapper(None)
        thirst = consume_goal(queue2, kind=GoalKind.SATISFY_THIRST, key="drink-key")
        bottle = drink_item("bottle")
        assert (
            wrapper2.propose_for_goal(
                to_planner_goal(thirst), observed(1, stats={"thirst": 0.8}, items=(bottle,))
            )
            is None
        )
        _, drink = take_request(channel2)
        assert drink.action is ActionName.CONSUME_DRINK
        assert drink.args["item_ref"] == bottle.ref

    def test_the_wrapped_planner_is_never_asked_about_a_consume_goal(self) -> None:
        spy = SpyPlanner()
        wrapper, queue, channel = bound_wrapper(spy)
        record = consume_goal(queue)

        wrapper.propose_for_goal(to_planner_goal(record), observed(1, items=(food_item("beans"),)))

        assert channel.pending_count == 1, "the mission served the goal itself"
        assert spy.goal_calls == [] and spy.propose_calls == 0

    def test_the_same_script_replayed_is_the_same_requests(self) -> None:
        crate = crate_ref(1202, 3400)
        beans = food_item("beans", container_ref=crate)

        def run() -> list[tuple[str, dict[str, Any]]]:
            wrapper, queue, channel = bound_wrapper()
            record = consume_goal(queue)
            goal = to_planner_goal(record)
            sent: list[tuple[str, dict[str, Any]]] = []
            for seq in (1, 2, 3):
                assert (
                    wrapper.propose_for_goal(
                        goal,
                        observed(
                            seq,
                            items=(beans,),
                            containers=(described_crate(crate),),
                            objects=(crate_object(crate),),
                        ),
                    )
                    is None
                )
                request = take_and_settle(channel, seq=seq)
                sent.append((request.action.value, dict(request.args)))
            return sent

        assert run() == run()

    def test_bounds_are_the_documented_ones(self) -> None:
        assert MAX_CANDIDATES_PER_MISSION == 8
        assert MAX_FETCH_DISTANCE == 30
        assert MAX_TRACKED_CONSUMES == 4
        # The wrapper's container-walk bound restates the memory store's own
        # hard ceiling; if the store's moves, this must be reviewed with it.
        assert CEILINGS["max_containers"] == _MAX_KNOWN_CONTAINERS

    def test_the_candidate_budget_ends_the_mission_typed(self) -> None:
        """One candidate allowed, one spent on a refusal: the next tick ends it."""
        wrapper, queue, channel = bound_wrapper(limits=ConsumeMissionLimits(max_candidates=1))
        record = consume_goal(queue)
        goal = to_planner_goal(record)
        beans = food_item("beans")

        assert wrapper.propose_for_goal(goal, observed(1, items=(beans,))) is None
        settle_failure(channel, seq=1, reason_code=ReasonCode.NO_SAFE_FOOD)

        assert wrapper.propose_for_goal(goal, observed(2, items=(beans,))) is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_SAFE_FOOD
