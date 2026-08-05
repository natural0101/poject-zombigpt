"""``movement.move_to`` and ``movement.move_near``.

Standing still is the default outcome of every test here: the adapter has to be
shown a character that actually arrived before it will say so. The rest of the
file is the ways it refuses — an unloaded square, a window, a drop, a floor it
cannot climb to, and a mod that reports the walk finished while the character is
exactly where it started.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
)
from pz_agent_core.actions.adapters import MOVE_RETRY_POLICY, MoveNearAdapter, MoveToAdapter
from pz_agent_core.actions.adapters.movement import (
    DEFAULT_ARRIVAL_RADIUS,
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
    SEMANTIC_BLOCKED,
    SEMANTIC_CLOSED_WINDOW,
    SEMANTIC_DROP,
    SEMANTIC_LOADED,
    SEMANTIC_STAIRS,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_observation, make_safety
from tests.fixtures.action_doubles import AckPlan, FakeClock, FakeCommandSink, FakeObservationSource
from tests.fixtures.adapter_worlds import (
    HOME_X,
    HOME_Y,
    a_command,
    a_square,
    a_world,
    a_world_object,
    object_ref,
    prepare,
    square_ref,
)

TARGET_X = HOME_X + 4
TARGET_Y = HOME_Y
CRATE = object_ref("crate-1")


def move_command(**args: object) -> Command:
    payload: dict[str, object] = {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 0}}
    payload.update(args)
    return a_command(ActionName.MOVEMENT_MOVE_TO, payload)


def world_with_target(
    *, semantics: list[str] | None = None, seq: int = 1, z: int = 0
) -> Observation:
    return a_world(seq=seq, objects=[a_square(TARGET_X, TARGET_Y, z, semantics=semantics)])


def standing_at(x: float, y: float, z: int = 0, seq: int = 2) -> Observation:
    return a_world(
        seq=seq,
        position=Position(x=x, y=y, z=z, direction="S"),
        objects=[a_square(TARGET_X, TARGET_Y)],
    )


def engine_for(
    adapter: MoveToAdapter,
    observations: list[Observation],
    *,
    repeated: Observation | None = None,
    acks: list[AckPlan] | None = None,
    capability: bool = True,
) -> tuple[ActionEngine, FakeCommandSink]:
    clock = FakeClock()
    sink = FakeCommandSink(clock, acks=acks or [])
    source = FakeObservationSource(clock)
    source.push(*observations)
    if repeated is not None:
        source.repeat(repeated)
    registry = AdapterRegistry()
    registry.register(adapter)
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _name: capability,
    )
    return engine, sink


def a_move_request(retries: int = 0) -> ActionRequest:
    return ActionRequest(
        action=ActionName.MOVEMENT_MOVE_TO,
        session_id=DEFAULT_SESSION,
        idempotency_key="move",
        args={"target": {"x": TARGET_X, "y": TARGET_Y, "z": 0}},
        policy=CommandPolicy(max_retries=retries),
    )


# --------------------------------------------------------------------------
# move_to: the postcondition
# --------------------------------------------------------------------------


def test_arrival_inside_the_radius_on_the_right_floor_is_the_evidence() -> None:
    adapter = MoveToAdapter()
    command = prepare(adapter, move_command(), world_with_target())

    evidence = adapter.verify(command, world_with_target(), standing_at(TARGET_X + 0.5, TARGET_Y))

    assert evidence is not None
    assert evidence.kind == "position_within_radius"
    assert evidence.observed["target"] == {"x": TARGET_X, "y": TARGET_Y, "z": 0}
    assert evidence.observed["distance"] <= DEFAULT_ARRIVAL_RADIUS


def test_standing_just_outside_the_radius_is_not_arrival() -> None:
    adapter = MoveToAdapter()
    command = prepare(adapter, move_command(), world_with_target())

    assert (
        adapter.verify(command, world_with_target(), standing_at(TARGET_X + 1.5, TARGET_Y)) is None
    )


def test_the_right_square_on_the_wrong_floor_is_not_arrival() -> None:
    """A character one storey above the target has not reached the target."""
    adapter = MoveToAdapter()
    command = prepare(adapter, move_command(), world_with_target())

    assert (
        adapter.verify(command, world_with_target(), standing_at(TARGET_X, TARGET_Y, z=1)) is None
    )


def test_the_prepared_command_carries_a_square_ref_and_never_permits_windows() -> None:
    adapter = MoveToAdapter()
    args = adapter.build_args(move_command(), world_with_target())

    assert args["square_ref"] == square_ref(TARGET_X, TARGET_Y)
    assert args["allow_windows"] is False


# --------------------------------------------------------------------------
# move_to: refusals, none of which send anything
# --------------------------------------------------------------------------


def test_an_unreported_square_is_target_not_loaded() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(), a_world(objects=[]))
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED


def test_a_reported_square_without_the_loaded_semantic_is_target_not_loaded() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(), world_with_target(semantics=[]))
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED


def test_a_blocked_square_has_no_path() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            move_command(), world_with_target(semantics=[SEMANTIC_LOADED, SEMANTIC_BLOCKED])
        )
    assert caught.value.reason_code is ReasonCode.PATH_NOT_FOUND


def test_a_closed_window_is_never_traversed_automatically() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            move_command(), world_with_target(semantics=[SEMANTIC_LOADED, SEMANTIC_CLOSED_WINDOW])
        )
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED
    assert "window" in str(caught.value)


def test_asking_for_windows_explicitly_is_still_refused() -> None:
    """``allow_windows`` is an argument the adapter accepts and always answers no to."""
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(allow_windows=True), world_with_target())
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED


def test_a_drop_from_height_is_never_taken() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            move_command(), world_with_target(semantics=[SEMANTIC_LOADED, SEMANTIC_DROP])
        )
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED
    assert "height" in str(caught.value)


def test_another_floor_without_stairs_has_no_path() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_TO, {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 1}}
    )

    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(command, world_with_target(z=1))
    assert caught.value.reason_code is ReasonCode.PATH_NOT_FOUND


def test_another_floor_reached_by_permitted_stairs_is_allowed() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_TO, {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 1}}
    )

    MoveToAdapter().validate(
        command, world_with_target(z=1, semantics=[SEMANTIC_LOADED, SEMANTIC_STAIRS])
    )


def test_stairs_the_caller_forbade_are_not_used() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_TO,
        {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 1}, "allow_stairs": False},
    )

    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            command, world_with_target(z=1, semantics=[SEMANTIC_LOADED, SEMANTIC_STAIRS])
        )
    assert caught.value.reason_code is ReasonCode.PATH_NOT_FOUND


def test_a_distant_target_is_out_of_range_rather_than_a_long_walk() -> None:
    far_x = HOME_X + MAX_MOVE_DISTANCE_SQUARES + 5
    command = a_command(
        ActionName.MOVEMENT_MOVE_TO,
        {"target": {"x": far_x, "y": HOME_Y, "z": 0}, "max_distance": MAX_MOVE_DISTANCE_SQUARES},
    )

    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(command, a_world(objects=[a_square(far_x, HOME_Y)]))
    assert caught.value.reason_code is ReasonCode.TARGET_OUT_OF_RANGE
    assert caught.value.evidence["max_distance"] == MAX_MOVE_DISTANCE_SQUARES


def test_the_single_command_distance_cap_cannot_be_raised_by_the_caller() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            move_command(max_distance=MAX_MOVE_DISTANCE_SQUARES + 1), world_with_target()
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_arrival_radius_cannot_be_widened_into_the_next_room() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(radius=MAX_ARRIVAL_RADIUS + 1), world_with_target())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "target",
    [
        {"x": 1.5, "y": 3400, "z": 0},
        {"x": TARGET_X, "y": TARGET_Y},
        "square:somewhere",
        {"x": True, "y": TARGET_Y, "z": 0},
    ],
)
def test_a_target_that_is_not_a_grid_square_is_refused(target: object) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(
            a_command(ActionName.MOVEMENT_MOVE_TO, {"target": target}), a_world()
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_argument_the_adapter_does_not_understand_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(sprint=True), world_with_target())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_movement_without_a_nearby_view_cannot_be_verified() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveToAdapter().validate(move_command(), a_world(nearby=NearbyView(), objects=[]))
    # An empty nearby view is a reported world with no squares in it, which is
    # a different fact from the mod not producing that tier at all.
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED

    without_tier = replace(a_world(), nearby=None)
    with pytest.raises(PreconditionFailed) as missing:
        MoveToAdapter().validate(move_command(), without_tier)
    assert missing.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# progress and risk
# --------------------------------------------------------------------------


def test_progress_is_the_fraction_of_the_gap_that_closed() -> None:
    adapter = MoveToAdapter()
    command = prepare(adapter, move_command(), world_with_target())
    start = world_with_target()

    assert adapter.progress(command, start, standing_at(HOME_X + 2, HOME_Y)) == pytest.approx(0.5)
    assert adapter.progress(command, start, standing_at(HOME_X, HOME_Y)) == 0.0


def test_a_move_that_leaves_the_safe_radius_needs_the_higher_tier() -> None:
    assert MoveToAdapter().risk is RiskClass.P2
    assert MoveToAdapter(safe_radius_squares=2).risk_for(move_command(), a_world()) is RiskClass.P3


def test_a_short_move_on_the_same_floor_stays_at_the_declared_tier() -> None:
    assert MoveToAdapter(safe_radius_squares=20).risk_for(move_command(), a_world()) is RiskClass.P2


def test_changing_floor_raises_the_tier() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_TO, {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 1}}
    )

    assert MoveToAdapter().risk_for(command, a_world()) is RiskClass.P3


def test_the_replan_budget_is_exactly_one() -> None:
    """§4.5 step 10: cancel and rebuild the path once, not until it works."""
    assert MOVE_RETRY_POLICY.max_retries == 1


# --------------------------------------------------------------------------
# through the engine
# --------------------------------------------------------------------------


def test_a_mod_that_acks_success_while_the_character_stands_still_fails() -> None:
    """A completed queue entry says nothing about where the character is."""
    stuck = world_with_target()
    engine, _sink = engine_for(
        MoveToAdapter(timeout_ms=2_000, poll_interval_ms=250),
        [stuck],
        repeated=stuck,
        acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)],
    )

    result = engine.execute(a_move_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


def test_an_arrival_observed_after_a_failure_ack_still_wins() -> None:
    arrived = standing_at(TARGET_X, TARGET_Y, seq=2)
    engine, _sink = engine_for(
        MoveToAdapter(timeout_ms=2_000, poll_interval_ms=250),
        [world_with_target(), arrived],
        repeated=arrived,
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_STUCK, after_polls=1)],
    )

    result = engine.execute(a_move_request())

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["kind"] == "position_within_radius"


def test_an_unverified_capability_stops_the_command_before_it_is_sent() -> None:
    engine, sink = engine_for(MoveToAdapter(), [world_with_target()], capability=False)

    result = engine.execute(a_move_request())

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert sink.sent == []


def test_manual_input_cancels_a_walk_in_flight() -> None:
    walking = world_with_target()
    taken_over = make_observation(
        seq=2,
        safety=make_safety(manual_takeover=True),
        nearby=walking.nearby,
        inventory=walking.inventory,
    )
    engine, sink = engine_for(
        MoveToAdapter(timeout_ms=2_000, poll_interval_ms=250),
        [walking, taken_over],
        repeated=taken_over,
    )

    result = engine.execute(a_move_request())

    assert result.status is ActionStatus.CANCELLED
    assert result.reason_code is ReasonCode.USER_TAKEOVER
    assert [reason for _, reason in sink.cancelled] == [ReasonCode.USER_TAKEOVER]


def test_a_stuck_walk_is_replanned_once_and_no_more() -> None:
    """PATH_STUCK is retryable, so the bound has to come from the policy."""
    stuck = world_with_target()
    engine, sink = engine_for(
        MoveToAdapter(timeout_ms=2_000, poll_interval_ms=250),
        [stuck],
        repeated=stuck,
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_STUCK, after_polls=1)],
    )

    result: ActionResult = engine.execute(
        ActionRequest(
            action=ActionName.MOVEMENT_MOVE_TO,
            session_id=DEFAULT_SESSION,
            idempotency_key="move",
            args={"target": {"x": TARGET_X, "y": TARGET_Y, "z": 0}},
            policy=MOVE_RETRY_POLICY,
        )
    )

    assert result.reason_code is ReasonCode.PATH_STUCK
    assert len(sink.sent) == 2


# --------------------------------------------------------------------------
# move_near
# --------------------------------------------------------------------------


def near_world(seq: int = 1, *, x: int = HOME_X + 3, distance: float | None = None) -> Observation:
    return a_world(
        seq=seq,
        objects=[a_world_object(CRATE, x=x, distance=distance), a_square(x, HOME_Y)],
    )


def near_command(**args: object) -> Command:
    payload: dict[str, object] = {"object_ref": CRATE}
    payload.update(args)
    return a_command(ActionName.MOVEMENT_MOVE_NEAR, payload)


def test_move_near_succeeds_only_once_the_object_is_within_reach() -> None:
    adapter = MoveNearAdapter()
    command = prepare(adapter, near_command(), near_world())

    far = a_world(
        seq=2,
        position=Position(x=float(HOME_X + 1), y=float(HOME_Y), z=0),
        objects=[a_world_object(CRATE, x=HOME_X + 3, distance=2.0)],
    )
    close = a_world(
        seq=3,
        position=Position(x=float(HOME_X + 3), y=float(HOME_Y), z=0),
        objects=[a_world_object(CRATE, x=HOME_X + 3, distance=0.0)],
    )

    assert adapter.verify(command, near_world(), far) is None
    evidence = adapter.verify(command, near_world(), close)
    assert evidence is not None
    assert evidence.observed["object_ref"] == CRATE


def test_move_near_is_not_verified_against_an_object_that_left_the_view() -> None:
    """After moving, refs are re-resolved; an object out of view proves nothing."""
    adapter = MoveNearAdapter()
    command = prepare(adapter, near_command(), near_world())
    gone = a_world(seq=2, position=Position(x=float(HOME_X + 3), y=float(HOME_Y), z=0), objects=[])

    assert adapter.verify(command, near_world(), gone) is None


def test_move_near_is_not_verified_from_another_floor() -> None:
    adapter = MoveNearAdapter()
    command = prepare(adapter, near_command(), near_world())
    upstairs = a_world(
        seq=2,
        position=Position(x=float(HOME_X + 3), y=float(HOME_Y), z=1),
        objects=[a_world_object(CRATE, x=HOME_X + 3, distance=0.0)],
    )

    assert adapter.verify(command, near_world(), upstairs) is None


def test_move_near_refuses_an_object_that_is_not_in_view() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        MoveNearAdapter().validate(near_command(), a_world(objects=[]))
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED


def test_move_near_refuses_an_object_reported_without_a_position() -> None:
    positionless: NearbyObject = replace(a_world_object(CRATE), position=None)

    with pytest.raises(PreconditionFailed) as caught:
        MoveNearAdapter().validate(near_command(), a_world(objects=[positionless]))
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED


def test_move_near_refuses_a_container_reference_in_place_of_an_object() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_NEAR, {"object_ref": f"container:{DEFAULT_SESSION}:player-main"}
    )

    with pytest.raises(PreconditionFailed) as caught:
        MoveNearAdapter().validate(command, near_world())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_move_near_refuses_a_reference_from_another_session() -> None:
    command = a_command(
        ActionName.MOVEMENT_MOVE_NEAR,
        {"object_ref": "object:11111111-2222-3333-4444-555555555555:9:0"},
    )

    with pytest.raises(PreconditionFailed) as caught:
        MoveNearAdapter().validate(command, near_world())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_approaching_a_world_object_is_always_the_world_tier() -> None:
    adapter = MoveNearAdapter()
    assert adapter.risk is RiskClass.P3
    assert adapter.risk_for(near_command(), near_world()) is RiskClass.P3
