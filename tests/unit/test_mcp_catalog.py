"""The published surface: the exact set, the gates it declares, and its schemas."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pz_agent_core.actions import AdapterRegistry, PreconditionFailed, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.adapters.movement import MAX_ARRIVAL_RADIUS, MAX_MOVE_DISTANCE_SQUARES
from pz_agent_core.capabilities.probes import (
    DOOR_TOGGLE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    INVENTORY_TRANSFER,
    MEDICAL_BANDAGE,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
)
from pz_agent_core.protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    ActionName,
    ReasonCode,
    RiskClass,
)
from pz_agent_mcp.catalog import (
    RESOURCES,
    TOOLS,
    TOOLS_BY_NAME,
    ToolKind,
    ToolSpec,
    published_tools,
    withheld_tools,
)
from pz_agent_mcp.envelope import ToolFailure
from pz_agent_mcp.validation import validate_arguments
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    item_ref,
    main_container_ref,
    make_observation,
)
from tests.fixtures.ipc_builders import make_command
from tests.fixtures.mcp_doubles import make_report

ITEM = item_ref("501", "player-main")
MAIN = main_container_ref()
BACKPACK = backpack_container_ref()
SQUARE = f"square:{DEFAULT_SESSION}:1200:3400:0"
CRATE = f"container:{DEFAULT_SESSION}:world:1200:3400:0:0:0"
DOOR = f"object:{DEFAULT_SESSION}:1200:3401:0:2"

#: The set named by docs/MCP_TOOLS.md, written out rather than derived, so a
#: tool appearing or vanishing has to be a deliberate edit in two places.
DOCUMENTED_TOOLS = {
    "pz_session_status",
    "pz_session_arm",
    "pz_session_disarm",
    "pz_goal_submit",
    "pz_goal_status",
    "pz_goal_cancel",
    "pz_observe_snapshot",
    "pz_observe_inventory",
    "pz_observe_nearby",
    "pz_action_inspect_world",
    "pz_action_inspect_container",
    "pz_action_search_inventory",
    "pz_action_move_to",
    "pz_action_move_near",
    "pz_action_open_container",
    "pz_action_open_door",
    "pz_action_close_door",
    "pz_action_unlock_door",
    "pz_action_transfer",
    "pz_action_ensure_main",
    "pz_action_eat",
    "pz_action_drink",
    "pz_action_drink_source",
    "pz_action_read",
    "pz_action_equip",
    "pz_action_unequip",
    "pz_action_bandage",
    "pz_action_rest",
    "pz_action_sleep",
    "pz_action_wait",
    "pz_action_cancel",
    "pz_action_cancel_all",
    "pz_action_status",
    "pz_action_await",
    "pz_plan_execute",
    "pz_plan_status",
    "pz_safety_stop",
    "pz_memory_query",
    "pz_debug_doctor",
    "pz_debug_tail",
}

DOCUMENTED_RESOURCES = {
    "pz://session/current",
    "pz://observation/latest",
    "pz://inventory/current",
    "pz://capabilities",
    "pz://plan/current",
    "pz://safety/status",
    "pz://diagnostics/recent",
}

ALL_CAPABILITIES = (
    MOVE_TO_SQUARE,
    DOOR_TOGGLE,
    INVENTORY_TRANSFER,
    EAT_PERCENTAGE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    READ_LITERATURE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    MEDICAL_BANDAGE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
)


def test_the_published_set_is_exactly_the_documented_one() -> None:
    assert set(TOOLS_BY_NAME) == DOCUMENTED_TOOLS
    assert {spec.uri for spec in RESOURCES} == DOCUMENTED_RESOURCES


def test_stop_and_disarm_never_require_an_armed_session() -> None:
    assert TOOLS_BY_NAME["pz_safety_stop"].requires_armed is False
    assert TOOLS_BY_NAME["pz_session_disarm"].requires_armed is False
    assert TOOLS_BY_NAME["pz_session_arm"].requires_armed is False


def test_every_action_tool_that_writes_requires_an_armed_session() -> None:
    writes = {"pz_action_move_to", "pz_action_transfer", "pz_action_eat", "pz_action_drink"}

    assert all(TOOLS_BY_NAME[name].requires_armed for name in writes)


def test_the_control_tools_are_the_protocols_always_allowed_set_plus_arming() -> None:
    control_actions = {
        spec.action for spec in TOOLS if spec.kind is ToolKind.CONTROL and spec.action is not None
    }

    assert control_actions <= ALWAYS_ALLOWED_ACTIONS


def test_read_tools_are_all_p0_and_submit_nothing() -> None:
    for spec in TOOLS:
        if spec.kind is not ToolKind.READ:
            continue
        assert spec.risk is RiskClass.P0
        assert spec.action is None
        assert spec.required_capability is None


def test_a_capability_backed_tool_is_withheld_when_the_capability_is_unsupported() -> None:
    report = make_report(usable=[INVENTORY_TRANSFER], unsupported=[EAT_PERCENTAGE])

    names = {spec.name for spec in published_tools(report)}

    assert "pz_action_transfer" in names
    assert "pz_action_eat" not in names
    assert withheld_tools(report)["pz_action_eat"] == "NO_VERIFIED_API"


def test_an_experimental_capability_is_not_published_as_ready() -> None:
    report = make_report(experimental=[READ_LITERATURE])

    assert "pz_action_read" not in {spec.name for spec in published_tools(report)}
    assert withheld_tools(report)["pz_action_read"] == "EXPERIMENTAL_API"


def test_a_capability_absent_from_the_report_is_withheld_not_assumed() -> None:
    report = make_report()

    withheld = withheld_tools(report)

    assert withheld["pz_action_move_to"] == "unsupported"


def test_tools_needing_no_capability_are_always_published() -> None:
    names = {spec.name for spec in published_tools(make_report())}

    assert {"pz_safety_stop", "pz_session_status", "pz_action_wait", "pz_action_cancel"} <= names


def test_every_capability_backed_tool_names_a_real_probe() -> None:
    named = {spec.required_capability for spec in TOOLS if spec.required_capability is not None}

    assert named <= set(ALL_CAPABILITIES)


def test_every_input_schema_is_closed_and_self_validating() -> None:
    for spec in TOOLS:
        assert spec.input_schema["additionalProperties"] is False
        # An empty payload either validates (everything optional) or is refused
        # for a named missing field; a SchemaError would mean the declaration
        # itself is unenforceable.
        try:
            validate_arguments(spec.input_schema, {})
        except ToolFailure as failure:
            assert failure.reason_code is ReasonCode.INVALID_ARGUMENT, spec.name


def test_no_schema_anywhere_accepts_code_a_path_or_a_keystroke() -> None:
    forbidden = {"lua", "script", "code", "command", "path", "file", "keys", "keystrokes", "steps"}

    for spec in TOOLS:
        assert not forbidden & set(spec.input_schema["properties"]), spec.name


def test_movement_bounds_match_the_adapter_that_will_receive_them() -> None:
    properties = TOOLS_BY_NAME["pz_action_move_to"].input_schema["properties"]

    assert properties["radius"]["maximum"] == MAX_ARRIVAL_RADIUS
    assert properties["max_distance"]["maximum"] == MAX_MOVE_DISTANCE_SQUARES


def test_allow_windows_is_not_offered_at_all() -> None:
    # The movement adapter refuses it with POLICY_DENIED; a boundary that
    # published it would advertise something policy forbids.
    assert "allow_windows" not in TOOLS_BY_NAME["pz_action_move_to"].input_schema["properties"]


def test_no_tool_offers_to_choose_which_item_to_consume() -> None:
    for name in ("pz_action_eat", "pz_action_drink", "pz_action_read"):
        properties = TOOLS_BY_NAME[name].input_schema["properties"]
        assert "selection" not in properties
        assert "item_ref" in TOOLS_BY_NAME[name].input_schema["required"]


def test_every_mutating_tool_carries_an_idempotency_key() -> None:
    for spec in TOOLS:
        if spec.action is None:
            continue
        assert "idempotency_key" in spec.input_schema["required"], spec.name


def test_no_tool_publishes_an_argument_its_handler_never_reads() -> None:
    # A plan is bounded by limits.max_real_seconds, not by a command lease, so
    # publishing timeout_ms on it would advertise an argument nothing reads —
    # the same lie as advertising a bound nothing enforces.
    assert "timeout_ms" not in TOOLS_BY_NAME["pz_plan_execute"].input_schema["properties"]
    for spec in TOOLS:
        if spec.action is None:
            continue
        assert "timeout_ms" in spec.input_schema["properties"], spec.name


def test_no_resource_is_advertised_as_subscribable_until_something_pushes_one() -> None:
    # build_server registers no subscribe handler and core publishes no
    # resource-change events; a client that subscribed would wait forever and
    # read the silence as "nothing has changed".
    assert not any(spec.subscribable for spec in RESOURCES)
    assert all(spec.descriptor()["subscribable"] is False for spec in RESOURCES)


def test_the_stop_tool_takes_no_arguments_so_nothing_can_make_it_fail() -> None:
    assert TOOLS_BY_NAME["pz_safety_stop"].input_schema["properties"] == {}


#: One filled-in payload per action tool: every optional argument the schema
#: offers, so the adapter sees the widest set of names this boundary can send.
FULL_ACTION_PAYLOADS: dict[str, dict[str, Any]] = {
    "pz_action_inspect_world": {"ref": SQUARE, "radius": 2},
    "pz_action_inspect_container": {"container_ref": MAIN, "limit": 10},
    "pz_action_search_inventory": {
        "full_type": "Base.Bandage",
        "type_prefix": "Base.",
        "edible": True,
        "drinkable": False,
        "readable": False,
        "exclude_equipped": True,
        "limit": 5,
    },
    "pz_action_move_to": {
        "target": {"x": 1210, "y": 3405, "z": 0},
        "radius": 1.5,
        "max_distance": 10,
        "allow_doors": True,
        "allow_stairs": False,
    },
    "pz_action_move_near": {
        "object_ref": CRATE,
        "radius": 1.5,
        "max_distance": 10,
        "allow_doors": False,
    },
    "pz_action_open_container": {"container_ref": CRATE, "radius": 1.5},
    "pz_action_open_door": {"door_ref": DOOR, "radius": 1.5},
    "pz_action_close_door": {"door_ref": DOOR, "radius": 1.5},
    "pz_action_unlock_door": {"door_ref": DOOR, "radius": 1.5},
    "pz_action_transfer": {
        "item_ref": ITEM,
        "destination_container_ref": BACKPACK,
        "source_container_ref": MAIN,
    },
    "pz_action_ensure_main": {"item_ref": ITEM},
    "pz_action_eat": {"item_ref": ITEM, "fraction": 0.5},
    "pz_action_drink": {"item_ref": ITEM, "fraction": 0.5},
    "pz_action_drink_source": {"item_ref": ITEM, "fraction": 0.5, "source_ref": SQUARE},
    "pz_action_read": {"item_ref": ITEM, "pages": 3},
    "pz_action_equip": {"item_ref": ITEM, "hand": "primary"},
    # All three namings at once, which the adapter refuses as a *domain* error —
    # "name what to take off exactly once". That is the refusal this check wants
    # to see: it proves check_args knows all three names, which naming only one
    # of them would leave untested for the other two.
    "pz_action_unequip": {"item_ref": ITEM, "hand": "primary", "slot": "Back"},
    "pz_action_bandage": {"body_part": "ForeArm_L", "item_ref": ITEM},
    "pz_action_rest": {
        "target_endurance": 0.95,
        "seat_ref": SQUARE,
        "allow_ground": True,
        "max_wait_ms": 60_000,
    },
    "pz_action_sleep": {
        "bed_ref": SQUARE,
        "hours": 6,
        "allow_vehicle_seat": True,
        "max_wait_ms": 120_000,
    },
    "pz_action_wait": {"game_seconds": 30},
    "pz_action_cancel": {"command_id": str(uuid.UUID(int=0xC0FFEE))},
}


def test_every_action_tool_is_covered_by_the_adapter_contract_check() -> None:
    # The check below is only as good as this table; an action tool added to the
    # catalogue and forgotten here would never meet its adapter.
    assert set(FULL_ACTION_PAYLOADS) == {spec.name for spec in TOOLS if spec.action is not None}


@pytest.mark.parametrize("tool", sorted(FULL_ACTION_PAYLOADS))
def test_every_schema_field_is_a_name_the_adapter_actually_accepts(tool: str) -> None:
    # The claim this boundary makes is that there is no translation table: the
    # schema field *is* the adapter argument. Nothing enforced that, so a
    # renamed adapter argument would have been discovered by the game refusing
    # every call. The adapter may refuse for any domain reason here — a missing
    # item, an unloaded square — but never because it was handed a name it does
    # not know.
    spec = TOOLS_BY_NAME[tool]
    assert spec.action is not None
    registry = register_game_adapters(register_builtins(AdapterRegistry()))
    args = {
        name: value
        for name, value in validate_arguments(
            spec.input_schema, {**FULL_ACTION_PAYLOADS[tool], "idempotency_key": "k1"}
        ).items()
        if name not in {"idempotency_key", "timeout_ms"}
    }
    command = make_command(spec.action, session_id=DEFAULT_SESSION, args=args)

    try:
        registry.get(spec.action).validate(command, make_observation())
    except PreconditionFailed as refusal:
        assert "unsupported argument" not in str(refusal), f"{tool}: {refusal}"


def test_a_long_running_tool_must_name_the_action_it_submits() -> None:
    with pytest.raises(ValueError, match="must name the action"):
        ToolSpec(
            name="pz_broken",
            kind=ToolKind.WRITE,
            risk=RiskClass.P2,
            summary="",
            input_schema={},
            long_running=True,
        )


def test_a_read_tool_may_not_submit_an_action() -> None:
    with pytest.raises(ValueError, match="must not submit an action"):
        ToolSpec(
            name="pz_broken",
            kind=ToolKind.READ,
            risk=RiskClass.P0,
            summary="",
            input_schema={},
            action=ActionName.CONSUME_EAT,
        )


def test_a_descriptor_tells_the_client_what_the_tool_needs() -> None:
    descriptor = TOOLS_BY_NAME["pz_action_eat"].descriptor()

    assert descriptor["requires_armed"] is True
    assert descriptor["long_running"] is True
    assert descriptor["capability"] == EAT_PERCENTAGE
    assert descriptor["risk"] == RiskClass.P2.value
