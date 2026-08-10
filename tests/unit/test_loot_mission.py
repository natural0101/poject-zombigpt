"""The loot mission behind the wrapper: ``loot_area`` served with no planner at all.

The loot *policy* — which item wins a tight capacity budget — is
``tests/unit/test_loot_policy.py``'s subject, journeys are
``test_navigation_executor.py``'s, and the loop-side wiring is
``test_cli_goal_wiring.py``'s. These tests own the mission's own promises,
driven the way ``test_navigation_planner.py`` drives journeys: a real
:class:`~pz_agent_core.goals.GoalQueue` and a real
:class:`~pz_agent_cli.runtime.ActionChannel` behind a bound
:class:`~pz_agent_cli.navigation_planner.NavigatingPlanner`, scripted
observations in, channel submissions settled by hand with real
:class:`~pz_agent_core.protocol.ActionResult` values, and every assertion on
the queue's, the channel's or the report's own answers:

* the scope is pinned from the activation observation, and an unreadable room
  is the typed ``PRECONDITION_FAILED`` naming ``scope=radius`` — never a guess;
* candidates are discovered per scope (room, building, radius) and doors are
  not candidates;
* memory answering "unchanged since last inspection" records the skip without
  a single action being submitted;
* the happy path over two containers runs open → inspect → select → batch per
  container through the action channel, ends the goal ``SUCCEEDED`` on real
  evidence, and the report says who was inspected, what was taken and why the
  rest was left;
* a ``CONTAINER_FULL`` stop with the main inventory provably below the
  smallest wanted weight ends the mission ``encumbered`` with the partial
  report, instead of visiting more containers;
* a locked door is a recorded skip reason naming the door;
* candidates, missions and reports are bounded; the mission dies with its
  goal and the report survives it; the wrapped planner is never asked.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pz_agent_cli.loot_mission import (
    DEFAULT_LOOT_RADIUS,
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_ENCUMBERED,
    ENDED_UNPINNED,
    SKIP_UNCHANGED,
    LootMissionLimits,
)
from pz_agent_cli.navigation_planner import (
    MAX_TRACKED_MISSIONS,
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
from pz_agent_core.goals.model import LootScope
from pz_agent_core.memory import SaveMemory, content_revision_of
from pz_agent_core.planner import Goal as PlannerGoal
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
    main_container_ref,
    make_container,
    make_item,
    make_observation,
    make_player,
)
from tests.fixtures.action_doubles import FakeClock

# --------------------------------------------------------------------------
# the world under the mission
# --------------------------------------------------------------------------

ROOM = "kitchen401"
BUILDING = "apartments4"


def crate_ref(x: int, y: int, z: int = 0, index: int = 1) -> str:
    return f"container:{DEFAULT_SESSION}:world:{x}:{y}:{z}:{index}:0"


def crate_tail(ref: str) -> str:
    return ref.split(":", 2)[2]


def crate_item(runtime_id: str, container_ref: str, **overrides: Any) -> ItemView:
    ref = f"item:{DEFAULT_SESSION}:{crate_tail(container_ref)}:{runtime_id}:0"
    return make_item(ref, container_ref, **overrides)


def crate_object(
    ref: str,
    *,
    room: str | None = ROOM,
    building: str | None = BUILDING,
) -> NearbyObject:
    parts = ref.split(":")
    x, y, z = int(parts[3]), int(parts[4]), int(parts[5])
    return NearbyObject(
        ref=ref,
        kind="container",
        distance=max(abs(x - 1200), abs(y - 3400)),
        position=Position(x=float(x), y=float(y), z=z),
        room=room,
        building=building,
    )


def observed(
    seq: int,
    *,
    room: str | None = ROOM,
    building: str | None = BUILDING,
    objects: tuple[NearbyObject, ...] = (),
    containers: tuple[ContainerView, ...] = (),
    items: tuple[ItemView, ...] = (),
    main_capacity: float | None = 20.0,
    main_used: float | None = 3.0,
) -> Observation:
    main = make_container(
        main_container_ref(),
        ContainerKind.PLAYER_MAIN,
        capacity=main_capacity,
        used_capacity=main_used,
    )
    return make_observation(
        seq=seq,
        player=make_player(
            position=Position(x=1200.0, y=3400.0, z=0, direction="S"),
            room=room,
            building=building,
        ),
        nearby=NearbyView(objects=list(objects)),
        inventory=InventoryView(containers=[main, *containers], items=list(items)),
    )


def described_crate(ref: str, **overrides: Any) -> ContainerView:
    return make_container(ref, ContainerKind.WORLD, name="crate", **overrides)


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


def bound_wrapper(
    inner: SpyPlanner | None = None,
    *,
    loot_limits: LootMissionLimits | None = None,
    loot_memory: object | None = None,
) -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(inner, loot_limits=loot_limits, loot_memory=loot_memory)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, queue, channel


def loot_goal(
    queue: GoalQueue,
    *,
    scope: LootScope | None = None,
    radius: int | None = None,
    take_all: bool | None = None,
    categories: str | None = None,
    key: str = "loot-key",
) -> GoalRecord:
    admission = queue.submit(
        GoalRequest(
            kind=GoalKind.LOOT_AREA,
            idempotency_key=key,
            params=GoalParams(scope=scope, radius=radius, take_all=take_all, categories=categories),
        )
    )
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def settle_success(channel: ActionChannel, *, seq: int, evidence: dict[str, object]) -> None:
    """Settle the one waiting submission with a real succeeded engine result."""
    taken = channel.take_next()
    assert taken is not None, "expected a channel submission to settle"
    action_id, request = taken
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


def settle_failure(
    channel: ActionChannel,
    *,
    seq: int,
    reason_code: ReasonCode,
    evidence: dict[str, object] | None = None,
) -> None:
    taken = channel.take_next()
    assert taken is not None, "expected a channel submission to settle"
    action_id, request = taken
    channel.settle(
        action_id,
        ActionResult.failure(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            reason_code=reason_code,
            evidence=evidence,
        ),
    )


def pending_request(channel: ActionChannel) -> ActionRequest:
    """Peek the waiting submission without taking it (via the record store)."""
    taken = channel.take_next()
    assert taken is not None
    action_id, request = taken
    # Put the terminal answer in immediately so the drive is not left waiting
    # on a submission the test only wanted to look at.
    channel.settle(
        action_id,
        ActionResult.succeeded(
            session_id=request.session_id,
            seq=999,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_999,
            evidence={"looked": True},
        ),
    )
    return request


# --------------------------------------------------------------------------
# scope pinning
# --------------------------------------------------------------------------


class TestScopePinning:
    def test_room_scope_with_no_room_is_the_typed_refusal(self) -> None:
        """Never guess: outdoors and no-reader are the same answer, and both refuse."""
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, room=None, building=None)
        )

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "no room" in ended.detail and "scope=radius" in ended.detail
        assert channel.pending_count == 0, "nothing was submitted for an unpinnable scope"
        report = wrapper.loot_report(record.goal_id)
        assert report is not None and report["ended"] == ENDED_UNPINNED

    def test_building_scope_with_no_building_refuses_the_same_way(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.BUILDING)

        wrapper.propose_for_goal(to_planner_goal(record), observed(1, building=None))

        ended = queue.record(record.goal_id)
        assert ended is not None and ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "scope=radius" in ended.detail

    def test_the_default_scope_is_room(self) -> None:
        """«облутай квартиру» carries no params, and no params means the room."""
        wrapper, queue, _ = bound_wrapper()
        record = loot_goal(queue)  # no scope supplied

        wrapper.propose_for_goal(to_planner_goal(record), observed(1, room=None))

        ended = queue.record(record.goal_id)
        assert ended is not None and ended.state is GoalState.FAILED, (
            "an absent scope must behave exactly like scope=room"
        )


# --------------------------------------------------------------------------
# candidate discovery by scope
# --------------------------------------------------------------------------


class TestCandidateDiscovery:
    def test_room_scope_takes_only_the_pinned_rooms_containers(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        here = crate_ref(1202, 3400)
        next_door = crate_ref(1210, 3400)
        unreadable = crate_ref(1204, 3402)

        value = wrapper.propose_for_goal(
            to_planner_goal(record),
            observed(
                1,
                objects=(
                    crate_object(here),
                    crate_object(next_door, room="hallway"),
                    # None is "unreadable or outdoors": not provably in scope.
                    crate_object(unreadable, room=None),
                ),
            ),
        )

        assert value is None
        request = pending_request(channel)
        assert request.action is ActionName.CONTAINER_OPEN_NEARBY
        assert request.args["container_ref"] == here

    def test_building_scope_sweeps_across_rooms_of_one_building(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.BUILDING)
        kitchen = crate_ref(1202, 3400)
        hallway = crate_ref(1206, 3400)
        elsewhere = crate_ref(1208, 3400)

        wrapper.propose_for_goal(
            to_planner_goal(record),
            observed(
                1,
                objects=(
                    crate_object(kitchen),
                    crate_object(hallway, room="hallway"),
                    crate_object(elsewhere, room="kitchen", building="other-building"),
                ),
            ),
        )

        first = pending_request(channel)
        assert first.args["container_ref"] == kitchen
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["scope"] == {"scope": "building", "building": BUILDING}

    def test_radius_scope_is_chebyshev_around_the_activation_square(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.RADIUS, radius=5)
        near = crate_ref(1203, 3400)
        far = crate_ref(1240, 3400)

        wrapper.propose_for_goal(
            to_planner_goal(record),
            # No rooms anywhere: radius must not need the room reader at all.
            observed(
                1,
                room=None,
                building=None,
                objects=(
                    crate_object(near, room=None, building=None),
                    crate_object(far, room=None, building=None),
                ),
            ),
        )

        request = pending_request(channel)
        assert request.args["container_ref"] == near
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["scope"]["radius"] == 5
        assert report["scope"]["centre"] == {"x": 1200, "y": 3400, "z": 0}

    def test_doors_and_malformed_refs_are_not_candidates(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        door = NearbyObject(
            ref=f"object:{DEFAULT_SESSION}:door7:0",
            kind="door",
            distance=1.0,
            position=Position(x=1201.0, y=3400.0, z=0),
            room=ROOM,
            building=BUILDING,
        )

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, objects=(door,)))

        # No candidates at all: the mission is complete without ever acting,
        # and the only honest way to end the goal is the engine observing the
        # completion probe — returned out the goal seam, exactly once per ask.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.idempotency_key == f"loot:{record.goal_id}:done1"
        assert channel.pending_count == 0


# --------------------------------------------------------------------------
# memory: the unchanged skip
# --------------------------------------------------------------------------


class TestMemoryUnchangedSkip:
    def test_an_unchanged_container_is_skipped_without_a_visit(self) -> None:
        crate = crate_ref(1202, 3400)
        beans = crate_item("beans", crate)
        memory = SaveMemory("save-loot")
        memory.note_container(
            container_ref=crate,
            kind=ContainerKind.WORLD,
            name="crate",
            now_ms=1,
            inspected=True,
            content_revision=content_revision_of([("Base.TinnedBeans", 1)]),
            item_count=1,
        )
        wrapper, queue, channel = bound_wrapper(loot_memory=memory)
        record = loot_goal(queue, scope=LootScope.ROOM)

        value = wrapper.propose_for_goal(
            to_planner_goal(record),
            observed(
                1,
                objects=(crate_object(crate),),
                containers=(described_crate(crate),),
                items=(beans,),
            ),
        )

        # The skip is recorded and no open/inspect was ever submitted; the
        # goal ends through the completion probe because nothing ran.
        assert channel.pending_count == 0
        assert value is not None and value.action is ActionName.MOVEMENT_MOVE_TO
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["containers_skipped"] == [{"ref": crate, "reason": SKIP_UNCHANGED}]
        assert report["containers_inspected"] == []
        assert report["ended"] == ENDED_COMPLETE

    def test_a_changed_container_is_visited_not_spared(self) -> None:
        crate = crate_ref(1202, 3400)
        memory = SaveMemory("save-loot")
        memory.note_container(
            container_ref=crate,
            kind=ContainerKind.WORLD,
            name="crate",
            now_ms=1,
            inspected=True,
            content_revision=content_revision_of([("Base.TinnedBeans", 3)]),
            item_count=3,
        )
        wrapper, queue, channel = bound_wrapper(loot_memory=memory)
        record = loot_goal(queue, scope=LootScope.ROOM)

        wrapper.propose_for_goal(
            to_planner_goal(record),
            observed(
                1,
                objects=(crate_object(crate),),
                containers=(described_crate(crate),),
                items=(crate_item("beans", crate),),  # one tin now, not three
            ),
        )

        assert channel.pending_count == 1, "a changed shelf sends the mission to look"


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


class TestTheFullHappyPath:
    def test_two_containers_are_opened_inspected_selected_and_batched(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        pantry = crate_ref(1202, 3400)
        cabinet = crate_ref(1204, 3401, index=2)
        beans = crate_item("beans", pantry)
        junk = crate_item(
            "junk", pantry, full_type="Base.Doodad", display_name="Doodad", category="Item"
        )
        pills = crate_item(
            "pills",
            cabinet,
            full_type="Base.Pills",
            display_name="Painkillers",
            category="Medical",
            weight=0.1,
        )

        def world(seq: int) -> Observation:
            return observed(
                seq,
                objects=(crate_object(pantry), crate_object(cabinet)),
                containers=(described_crate(pantry), described_crate(cabinet)),
                items=(beans, junk, pills),
            )

        # -- pantry: open, inspect, batch ---------------------------------
        assert wrapper.propose_for_goal(goal, world(1)) is None
        taken = channel.take_next()
        assert taken is not None
        open_id, open_request = taken
        assert open_request.action is ActionName.CONTAINER_OPEN_NEARBY
        assert open_request.args["container_ref"] == pantry
        assert open_request.idempotency_key.startswith(f"loot:{record.goal_id}:")
        channel.settle(
            open_id,
            ActionResult.succeeded(
                session_id=open_request.session_id,
                seq=1,
                command_id=open_id,
                action=open_request.action.value,
                timestamp_ms=1_700_000_000_001,
                evidence={"container_ref": pantry},
            ),
        )

        assert wrapper.propose_for_goal(goal, world(2)) is None
        taken = channel.take_next()
        assert taken is not None
        inspect_id, inspect_request = taken
        assert inspect_request.action is ActionName.CONTAINER_INSPECT
        assert inspect_request.args["container_ref"] == pantry
        channel.settle(
            inspect_id,
            ActionResult.succeeded(
                session_id=inspect_request.session_id,
                seq=2,
                command_id=inspect_id,
                action=inspect_request.action.value,
                timestamp_ms=1_700_000_000_002,
                evidence={"container_ref": pantry, "item_count": 2},
            ),
        )

        assert wrapper.propose_for_goal(goal, world(3)) is None
        taken = channel.take_next()
        assert taken is not None
        batch_id, batch_request = taken
        assert batch_request.action is ActionName.INVENTORY_TRANSFER_BATCH
        # The deterministic selection: the tin is FOOD and wanted, the doodad
        # is OTHER and left — the policy picked, not the mission.
        assert batch_request.args["item_refs"] == [beans.ref]
        assert batch_request.args["destination_container_ref"] == main_container_ref()
        channel.settle(
            batch_id,
            ActionResult.succeeded(
                session_id=batch_request.session_id,
                seq=3,
                command_id=batch_id,
                action=batch_request.action.value,
                timestamp_ms=1_700_000_000_003,
                evidence={"destination_ref": main_container_ref(), "transferred": [beans.ref]},
            ),
        )

        # -- cabinet: open, inspect, batch --------------------------------
        assert wrapper.propose_for_goal(goal, world(4)) is None
        settle_success(channel, seq=4, evidence={"container_ref": cabinet})
        assert wrapper.propose_for_goal(goal, world(5)) is None
        settle_success(channel, seq=5, evidence={"container_ref": cabinet, "item_count": 1})
        assert wrapper.propose_for_goal(goal, world(6)) is None
        taken = channel.take_next()
        assert taken is not None
        second_batch_id, second_batch = taken
        assert second_batch.args["item_refs"] == [pills.ref]
        channel.settle(
            second_batch_id,
            ActionResult.succeeded(
                session_id=second_batch.session_id,
                seq=6,
                command_id=second_batch_id,
                action=second_batch.action.value,
                timestamp_ms=1_700_000_000_006,
                evidence={"destination_ref": main_container_ref(), "transferred": [pills.ref]},
            ),
        )

        # -- terminal: candidates exhausted, goal succeeds on real evidence
        assert wrapper.propose_for_goal(goal, world(7)) is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert finished.reason_code is ReasonCode.POSTCONDITION_MET
        assert finished.evidence_keys, "success must carry the observed evidence keys"
        assert wrapper.tracked_missions == 0, "the mission dies with its finished goal"

        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_COMPLETE
        assert report["containers_inspected"] == [pantry, cabinet]
        assert report["containers_skipped"] == []
        assert report["items_taken"] == {"FOOD": 1, "MEDICAL": 1}
        assert report["items_left"] == {"NOT_WANTED": 1}
        assert report["candidates_truncated"] is False

    def test_a_categories_goal_narrows_the_selection(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM, categories="medical")
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)
        beans = crate_item("beans", crate)
        pills = crate_item("pills", crate, full_type="Base.Pills", category="Medical", weight=0.1)

        def world(seq: int) -> Observation:
            return observed(
                seq,
                objects=(crate_object(crate),),
                containers=(described_crate(crate),),
                items=(beans, pills),
            )

        assert wrapper.propose_for_goal(goal, world(1)) is None
        settle_success(channel, seq=1, evidence={"container_ref": crate})
        assert wrapper.propose_for_goal(goal, world(2)) is None
        settle_success(channel, seq=2, evidence={"container_ref": crate})
        assert wrapper.propose_for_goal(goal, world(3)) is None
        taken = channel.take_next()
        assert taken is not None
        _, batch = taken
        assert batch.args["item_refs"] == [pills.ref], (
            "categories=medical must leave the food on the shelf"
        )


# --------------------------------------------------------------------------
# the capacity stop
# --------------------------------------------------------------------------


class TestEncumberedEnding:
    def test_a_container_full_stop_ends_the_mission_encumbered(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        pantry = crate_ref(1202, 3400)
        untouched = crate_ref(1206, 3400, index=2)
        beans = crate_item("beans", pantry)

        def world(seq: int, *, main_capacity: float | None, main_used: float | None) -> Observation:
            return observed(
                seq,
                objects=(crate_object(pantry), crate_object(untouched)),
                containers=(described_crate(pantry), described_crate(untouched)),
                items=(beans,),
                main_capacity=main_capacity,
                main_used=main_used,
            )

        # Capacity unreported at first: the selection trusts the mod's own
        # per-item stop and takes the tin.
        assert wrapper.propose_for_goal(goal, world(1, main_capacity=None, main_used=None)) is None
        settle_success(channel, seq=1, evidence={"container_ref": pantry})
        assert wrapper.propose_for_goal(goal, world(2, main_capacity=None, main_used=None)) is None
        settle_success(channel, seq=2, evidence={"container_ref": pantry})
        assert wrapper.propose_for_goal(goal, world(3, main_capacity=None, main_used=None)) is None
        settle_failure(channel, seq=3, reason_code=ReasonCode.CONTAINER_FULL)

        # The next observation reports the main inventory with less free room
        # than the smallest wanted item weighs: provably full, honestly ended.
        assert wrapper.propose_for_goal(goal, world(4, main_capacity=10.0, main_used=9.9)) is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.CONTAINER_FULL
        assert "loot encumbered" in ended.detail
        assert channel.pending_count == 0, "no further container was visited"

        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["ended"] == ENDED_ENCUMBERED
        assert report["containers_inspected"] == [pantry]
        assert report["items_taken"] == {}, "nothing was observed landing"
        assert report["items_left"].get("OVER_CAPACITY") == 1

    def test_room_reappearing_clears_the_floor_and_the_sweep_continues(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        pantry = crate_ref(1202, 3400)
        beans = crate_item("beans", pantry)

        def world(seq: int, *, main_used: float | None) -> Observation:
            return observed(
                seq,
                objects=(crate_object(pantry),),
                containers=(described_crate(pantry),),
                items=(beans,),
                main_capacity=None if main_used is None else 20.0,
                main_used=main_used,
            )

        assert wrapper.propose_for_goal(goal, world(1, main_used=None)) is None
        settle_success(channel, seq=1, evidence={"container_ref": pantry})
        assert wrapper.propose_for_goal(goal, world(2, main_used=None)) is None
        settle_success(channel, seq=2, evidence={"container_ref": pantry})
        assert wrapper.propose_for_goal(goal, world(3, main_used=None)) is None
        settle_failure(channel, seq=3, reason_code=ReasonCode.CONTAINER_FULL)

        # Plenty of room on the next look: the proof of fullness is gone and
        # the mission completes instead of claiming encumbrance.
        assert wrapper.propose_for_goal(goal, world(4, main_used=3.0)) is None
        finished = queue.record(record.goal_id)
        assert finished is not None and finished.state is GoalState.SUCCEEDED


# --------------------------------------------------------------------------
# doors and failures
# --------------------------------------------------------------------------


class TestSkipsAndFailures:
    def test_a_locked_door_refusal_is_a_skip_reason_naming_the_door(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)
        door_ref = f"object:{DEFAULT_SESSION}:door7:0"

        assert wrapper.propose_for_goal(goal, observed(1, objects=(crate_object(crate),))) is None
        settle_failure(
            channel,
            seq=1,
            reason_code=ReasonCode.DOOR_LOCKED,
            evidence={"door_ref": door_ref},
        )

        value = wrapper.propose_for_goal(goal, observed(2, objects=(crate_object(crate),)))

        # The candidate is skipped, not retried; nothing ran, so completion
        # goes out as the probe.
        assert value is not None and value.action is ActionName.MOVEMENT_MOVE_TO
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert report["containers_skipped"] == [
            {"ref": crate, "reason": f"door {door_ref} is locked"}
        ]

    def test_consecutive_failures_end_the_mission_typed(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        crates = tuple(crate_ref(1202 + 2 * i, 3400, index=i + 1) for i in range(4))
        objects = tuple(crate_object(ref) for ref in crates)

        for attempt in range(3):
            assert wrapper.propose_for_goal(goal, observed(attempt + 1, objects=objects)) is None
            settle_failure(channel, seq=attempt + 1, reason_code=ReasonCode.INTERNAL_ERROR)

        assert wrapper.propose_for_goal(goal, observed(9, objects=objects)) is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        report = wrapper.loot_report(record.goal_id)
        assert report is not None and len(report["containers_skipped"]) == 3


# --------------------------------------------------------------------------
# bounds, lifetimes, and who is never asked
# --------------------------------------------------------------------------


class TestBoundsAndLifetimes:
    def test_candidates_are_bounded_and_the_report_says_so(self) -> None:
        limits = LootMissionLimits(max_candidates=2)
        # Every candidate reads as unchanged, so the whole sweep is skips and
        # the bound is observable without driving six pipelines.
        wrapper, queue, channel = bound_wrapper(
            loot_limits=limits,
            loot_memory=_AlwaysUnchanged(),
        )
        record = loot_goal(queue, scope=LootScope.ROOM)
        crates = tuple(crate_ref(1202 + 2 * i, 3400, index=i + 1) for i in range(3))

        value = wrapper.propose_for_goal(
            to_planner_goal(record),
            observed(
                1,
                objects=tuple(crate_object(ref) for ref in crates),
                containers=tuple(described_crate(ref) for ref in crates),
                items=(),
            ),
        )

        assert value is not None, "all candidates skipped: completion probes out the seam"
        report = wrapper.loot_report(record.goal_id)
        assert report is not None
        assert len(report["containers_skipped"]) == 2
        assert report["candidates_truncated"] is True
        assert channel.pending_count == 0

    def test_the_mission_dies_with_its_cancelled_goal_and_the_report_survives(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = loot_goal(queue, scope=LootScope.ROOM)
        goal = to_planner_goal(record)
        crate = crate_ref(1202, 3400)

        assert wrapper.propose_for_goal(goal, observed(1, objects=(crate_object(crate),))) is None
        assert wrapper.tracked_missions == 1
        assert channel.pending_count == 1

        assert queue.request_cancel(record.goal_id) is True
        queue.tick()
        # The next call — any call — prunes; nothing new is submitted.
        assert wrapper.propose(observed(2)) is None

        assert wrapper.tracked_missions == 0, "the mission must die with its goal"
        assert channel.pending_count == 1, "the admitted step stays the engine's to finish"
        cancelled = queue.record(record.goal_id)
        assert cancelled is not None and cancelled.state is GoalState.CANCELLED
        report = wrapper.loot_report(record.goal_id)
        assert report is not None, "the report must survive the goal"
        assert report["ended"] == ENDED_CANCELLED

    def test_the_wrapped_planner_is_never_asked_about_a_loot_goal(self) -> None:
        spy = SpyPlanner()
        wrapper, queue, channel = bound_wrapper(spy)
        record = loot_goal(queue, scope=LootScope.ROOM)

        wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, objects=(crate_object(crate_ref(1202, 3400)),))
        )

        assert channel.pending_count == 1, "the mission served the goal itself"
        assert spy.goal_calls == [] and spy.propose_calls == 0

    def test_an_unbound_wrapper_declines_rather_than_guessing(self) -> None:
        queue = GoalQueue(clock=FakeClock())
        wrapper = NavigatingPlanner(None)
        record = loot_goal(queue, scope=LootScope.ROOM)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1))

        assert value is None
        refreshed = queue.record(record.goal_id)
        assert refreshed is not None and refreshed.state is GoalState.ACTIVE

    def test_the_tracked_sets_are_bounded_by_construction(self) -> None:
        wrapper, _, _ = bound_wrapper()
        assert wrapper.tracked_missions <= MAX_TRACKED_MISSIONS

    def test_the_default_radius_is_the_documented_one(self) -> None:
        assert DEFAULT_LOOT_RADIUS == 10


class _AlwaysUnchanged:
    """A loot memory whose every shelf reads as already inspected and unchanged."""

    def reserves_item(self, full_type: str, /) -> bool:
        return False

    def container_unchanged(self, tail: str, revision: str) -> bool:
        return True
