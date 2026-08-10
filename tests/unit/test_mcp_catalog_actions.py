"""The seventeen-action surface: its bounds, its arming, and its examples.

``test_mcp_catalog.py`` covers the shape of the catalogue as a whole. This file
is about the declarations the action tools make, and it asks three questions of
each of them.

*Is the bound stated where a client can see it?* An adapter that refuses a
radius of 40 and a schema that advertises no ceiling are a tool that fails on
the first call and cannot be checked before making it. So every numeric argument
is bounded in the declaration, and every ceiling is the adapter's own number.

*Is the arming right?* ``requires_armed`` is derived from
:class:`~pz_agent_mcp.catalog.ToolKind`, and the kind is a hand-written field. A
mistake in the permissive direction is the one that matters: an unarmed session
that can reach ``container.open_nearby`` is an unarmed session that walks the
character across a room. The protocol's
:data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS` is the authority here, never
the tool's name.

*Would the example work?* Examples are the first thing a client copies. Each one
is validated against its own schema at import; what is checked here is the half
that import cannot see — that the values are references of the right kind, in
the right shape, and that the enums a tool publishes are not wider than the ones
the adapter accepts.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from pz_agent_core.actions import AdapterRegistry, PreconditionFailed, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.adapters.container import (
    DEFAULT_OPEN_RADIUS,
    MAX_LISTED_ITEMS,
    MIN_OPEN_RADIUS,
)
from pz_agent_core.actions.adapters.doors import DEFAULT_DOOR_RADIUS, MIN_DOOR_RADIUS
from pz_agent_core.actions.adapters.equipment import HANDS, MAX_SLOT_NAME_LEN
from pz_agent_core.actions.adapters.inventory import MAX_SEARCH_RESULTS, MAX_TYPE_FILTER_LEN
from pz_agent_core.actions.adapters.medical import BODY_PARTS
from pz_agent_core.actions.adapters.movement import (
    DEFAULT_NEAR_RADIUS,
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
)
from pz_agent_core.actions.adapters.survival import (
    MAX_REST_TARGET,
    MAX_REST_WAIT_MS,
    MAX_SLEEP_HOURS,
    MAX_SLEEP_WAIT_MS,
    MIN_REST_TARGET,
    MIN_SLEEP_HOURS,
    MIN_WAIT_MS,
)
from pz_agent_core.actions.adapters.world import MAX_INSPECT_RADIUS
from pz_agent_core.capabilities.probes import SURVIVAL_SLEEP
from pz_agent_core.protocol import READ_ONLY_ACTIONS, ActionName, ReasonCode, RiskClass
from pz_agent_mcp.catalog import (
    EXAMPLE_SESSION_ID,
    TOOLS,
    TOOLS_BY_NAME,
    ToolKind,
    ToolSpec,
    published_tools,
    withheld_tools,
)
from pz_agent_mcp.envelope import ToolFailure
from pz_agent_mcp.validation import validate_arguments
from tests.fixtures import make_observation
from tests.fixtures.ipc_builders import make_command
from tests.fixtures.mcp_doubles import make_report

#: The tools this file was written for: everything that submits an action.
ACTION_TOOLS: Final[tuple[ToolSpec, ...]] = tuple(spec for spec in TOOLS if spec.action is not None)

#: The three that only look. Written out rather than derived from the catalogue,
#: because deriving them from the field under test would make the check agree
#: with itself.
QUERY_TOOLS: Final[frozenset[str]] = frozenset(
    {"pz_action_inspect_world", "pz_action_inspect_container", "pz_action_search_inventory"}
)

#: Fields that are the command envelope rather than adapter arguments.
ENVELOPE: Final[frozenset[str]] = frozenset({"idempotency_key", "timeout_ms"})


def properties(tool: str) -> dict[str, Any]:
    schema: dict[str, Any] = TOOLS_BY_NAME[tool].input_schema["properties"]
    return schema


def registry() -> AdapterRegistry:
    return register_game_adapters(register_builtins(AdapterRegistry()))


# -- bounds ----------------------------------------------------------------


@pytest.mark.parametrize("spec", ACTION_TOOLS, ids=lambda spec: spec.name)
def test_every_number_an_action_tool_accepts_has_a_ceiling(spec: ToolSpec) -> None:
    """Unbounded is a bug by house rule, and a ceiling only the adapter knows is unpublished.

    A caller cannot plan against a limit it is not told about: it finds out by
    having a command refused, which on a long-running action costs a round trip
    through the game.
    """
    for name, declared in spec.input_schema["properties"].items():
        if declared["type"] not in {"integer", "number"}:
            continue
        assert "maximum" in declared, f"{spec.name}.{name} has no ceiling"
        assert "minimum" in declared or "exclusiveMinimum" in declared, (
            f"{spec.name}.{name} has no floor"
        )


@pytest.mark.parametrize("spec", ACTION_TOOLS, ids=lambda spec: spec.name)
def test_every_string_an_action_tool_accepts_is_closed_or_bounded(spec: ToolSpec) -> None:
    for name, declared in spec.input_schema["properties"].items():
        if declared["type"] != "string":
            continue
        assert "enum" in declared or "maxLength" in declared, f"{spec.name}.{name} is unbounded"


#: Tool, argument, and the adapter constant the schema must be quoting. The
#: numbers are imported from the adapters rather than typed out: a test that
#: restated them would pass while both drifted together.
DECLARED_BOUNDS: Final[tuple[tuple[str, str, str, Any], ...]] = (
    ("pz_action_inspect_world", "radius", "maximum", MAX_INSPECT_RADIUS),
    ("pz_action_inspect_container", "limit", "maximum", MAX_LISTED_ITEMS),
    ("pz_action_search_inventory", "limit", "maximum", MAX_SEARCH_RESULTS),
    ("pz_action_search_inventory", "full_type", "maxLength", MAX_TYPE_FILTER_LEN),
    ("pz_action_search_inventory", "type_prefix", "maxLength", MAX_TYPE_FILTER_LEN),
    ("pz_action_move_near", "radius", "maximum", MAX_ARRIVAL_RADIUS),
    ("pz_action_move_near", "max_distance", "maximum", MAX_MOVE_DISTANCE_SQUARES),
    ("pz_action_move_near", "radius", "default", DEFAULT_NEAR_RADIUS),
    ("pz_action_open_container", "radius", "maximum", MAX_ARRIVAL_RADIUS),
    ("pz_action_open_container", "radius", "minimum", MIN_OPEN_RADIUS),
    ("pz_action_open_container", "radius", "default", DEFAULT_OPEN_RADIUS),
    ("pz_action_open_door", "radius", "maximum", MAX_ARRIVAL_RADIUS),
    ("pz_action_open_door", "radius", "minimum", MIN_DOOR_RADIUS),
    ("pz_action_open_door", "radius", "default", DEFAULT_DOOR_RADIUS),
    ("pz_action_close_door", "radius", "maximum", MAX_ARRIVAL_RADIUS),
    ("pz_action_unlock_door", "radius", "maximum", MAX_ARRIVAL_RADIUS),
    ("pz_action_unequip", "slot", "maxLength", MAX_SLOT_NAME_LEN),
    ("pz_action_rest", "target_endurance", "minimum", MIN_REST_TARGET),
    ("pz_action_rest", "target_endurance", "maximum", MAX_REST_TARGET),
    ("pz_action_rest", "max_wait_ms", "minimum", MIN_WAIT_MS),
    ("pz_action_rest", "max_wait_ms", "maximum", MAX_REST_WAIT_MS),
    ("pz_action_sleep", "hours", "minimum", MIN_SLEEP_HOURS),
    ("pz_action_sleep", "hours", "maximum", MAX_SLEEP_HOURS),
    ("pz_action_sleep", "max_wait_ms", "minimum", MIN_WAIT_MS),
    ("pz_action_sleep", "max_wait_ms", "maximum", MAX_SLEEP_WAIT_MS),
)


@pytest.mark.parametrize(("tool", "argument", "keyword", "expected"), DECLARED_BOUNDS)
def test_a_published_bound_is_the_adapters_own_number(
    tool: str, argument: str, keyword: str, expected: Any
) -> None:
    assert properties(tool)[argument][keyword] == expected


#: One value past each ceiling. A bound that is advertised and not enforced is
#: the failure mode this catches: the schema is the only statement of the
#: contract, so the validator reading it is what makes the statement true.
OVER_THE_LINE: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("pz_action_inspect_world", {"radius": MAX_INSPECT_RADIUS + 1}),
    ("pz_action_inspect_container", {"limit": MAX_LISTED_ITEMS + 1}),
    ("pz_action_search_inventory", {"limit": MAX_SEARCH_RESULTS + 1}),
    ("pz_action_search_inventory", {"full_type": "x" * (MAX_TYPE_FILTER_LEN + 1)}),
    ("pz_action_search_inventory", {"full_type": "Base.Tinned Beans"}),
    ("pz_action_move_near", {"radius": MAX_ARRIVAL_RADIUS + 0.5}),
    ("pz_action_move_near", {"max_distance": MAX_MOVE_DISTANCE_SQUARES + 1}),
    ("pz_action_open_container", {"radius": MAX_ARRIVAL_RADIUS + 0.5}),
    ("pz_action_open_container", {"radius": MIN_OPEN_RADIUS / 2}),
    ("pz_action_equip", {"hand": "third"}),
    ("pz_action_unequip", {"hand": "both"}),
    ("pz_action_unequip", {"slot": "Back:evil"}),
    ("pz_action_bandage", {"body_part": "Tentacle"}),
    ("pz_action_rest", {"target_endurance": MAX_REST_TARGET + 0.1}),
    ("pz_action_rest", {"max_wait_ms": MAX_REST_WAIT_MS + 1}),
    ("pz_action_sleep", {"hours": MAX_SLEEP_HOURS + 1}),
    ("pz_action_sleep", {"max_wait_ms": MAX_SLEEP_WAIT_MS + 1}),
    ("pz_action_sleep", {"hours": MIN_SLEEP_HOURS - 1}),
    ("pz_action_drink_source", {"fraction": 1.5}),
    ("pz_action_drink_source", {"source_ref": "container:not-a-square"}),
    ("pz_action_open_door", {"radius": MAX_ARRIVAL_RADIUS + 0.5}),
    ("pz_action_open_door", {"door_ref": "container:not-a-door"}),
    ("pz_action_close_door", {"radius": MIN_DOOR_RADIUS / 2}),
    ("pz_action_unlock_door", {"radius": MAX_ARRIVAL_RADIUS + 0.5}),
)


@pytest.mark.parametrize(("tool", "overshoot"), OVER_THE_LINE)
def test_a_value_past_a_published_bound_is_refused_here(
    tool: str, overshoot: dict[str, Any]
) -> None:
    spec = TOOLS_BY_NAME[tool]
    payload = {**spec.example, **overshoot}

    with pytest.raises(ToolFailure) as refused:
        validate_arguments(spec.input_schema, payload)

    assert refused.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_every_bounded_argument_of_a_new_tool_is_exercised_by_the_overshoot_table() -> None:
    """The table above is the check; a tool missing from it proves nothing.

    Only the bounded arguments count — a boolean has nothing to overshoot and a
    reference is checked by its pattern rather than by a range.
    """
    exercised = {tool for tool, _ in OVER_THE_LINE}
    bounded = {
        spec.name
        for spec in ACTION_TOOLS
        if any(
            name not in ENVELOPE
            and ("maximum" in declared or "enum" in declared or "pattern" in declared)
            for name, declared in spec.input_schema["properties"].items()
        )
    }

    assert bounded - exercised <= {
        # Covered by test_mcp_catalog.py, which owns the original seven.
        "pz_action_move_to",
        "pz_action_transfer",
        "pz_action_ensure_main",
        "pz_action_eat",
        "pz_action_drink",
        "pz_action_read",
        "pz_action_wait",
        "pz_action_cancel",
    }


# -- what needs arming -----------------------------------------------------


def test_the_query_tools_are_exactly_the_read_only_actions_that_are_published() -> None:
    declared_query = {spec.name for spec in TOOLS if spec.kind is ToolKind.QUERY}

    assert declared_query == QUERY_TOOLS
    assert {TOOLS_BY_NAME[name].action for name in QUERY_TOOLS} <= READ_ONLY_ACTIONS


@pytest.mark.parametrize("tool", sorted(QUERY_TOOLS))
def test_a_query_tool_needs_no_arming_and_claims_no_capability(tool: str) -> None:
    spec = TOOLS_BY_NAME[tool]

    assert spec.requires_armed is False
    assert spec.risk is RiskClass.P0
    # Every probe resolves a Lua symbol; what a look needs is an observation
    # tier, and its adapter refuses with CAPABILITY_UNAVAILABLE when the mod
    # produced none. A probe here would report 'unsupported' on a healthy build.
    assert spec.required_capability is None


def test_opening_a_container_is_not_a_query_however_its_name_reads() -> None:
    spec = TOOLS_BY_NAME["pz_action_open_container"]

    assert spec.action is ActionName.CONTAINER_OPEN_NEARBY
    assert spec.action not in READ_ONLY_ACTIONS
    assert spec.kind is ToolKind.WRITE
    assert spec.requires_armed is True


def test_a_query_tool_that_named_a_mutating_action_cannot_be_constructed() -> None:
    # The one mistake here that would be silent: a query tool skips the arming
    # gate, so an action that moves the character wearing this kind would move
    # it on a disarmed session.
    with pytest.raises(ValueError, match="must submit a read-only action"):
        ToolSpec(
            name="pz_broken",
            kind=ToolKind.QUERY,
            risk=RiskClass.P0,
            summary="",
            input_schema={"type": "object", "additionalProperties": False, "properties": {}},
            action=ActionName.CONTAINER_OPEN_NEARBY,
        )


# -- the enums, against the adapters that own them -------------------------


def test_the_hand_enums_are_the_adapters_own_vocabularies() -> None:
    assert properties("pz_action_equip")["hand"]["enum"] == sorted(HANDS)
    # 'both' is absent from unequip on purpose: a hand is emptied one at a time,
    # and "take off both" is two commands with two results.
    assert properties("pz_action_unequip")["hand"]["enum"] == ["primary", "secondary"]


def test_the_body_part_enum_is_the_closed_set_the_adapter_looks_wounds_up_in() -> None:
    assert set(properties("pz_action_bandage")["body_part"]["enum"]) == BODY_PARTS


def test_the_unequip_enum_is_not_wider_than_the_adapter_accepts() -> None:
    """``both`` is published for equip and refused for unequip; prove the second half.

    An enum copied from the wrong adapter would pass every schema check here and
    be refused by the game on the first call.
    """
    command = make_command(
        ActionName.EQUIPMENT_UNEQUIP, session_id=EXAMPLE_SESSION_ID, args={"hand": "both"}
    )

    with pytest.raises(PreconditionFailed) as refused:
        registry().get(ActionName.EQUIPMENT_UNEQUIP).validate(command, make_observation())

    assert refused.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_three_search_filters_have_no_default_because_absent_is_not_false() -> None:
    # Absent means "do not filter on this"; false means "must not be edible".
    # A default would silently narrow every search that left one out, and the
    # narrowing would be invisible in the answer.
    for name in ("edible", "drinkable", "readable"):
        assert "default" not in properties("pz_action_search_inventory")[name]


# -- the examples ----------------------------------------------------------


@pytest.mark.parametrize("spec", ACTION_TOOLS, ids=lambda spec: spec.name)
def test_every_action_tool_carries_an_example_that_names_a_key(spec: ToolSpec) -> None:
    # The schema already refuses an example it cannot validate, at import. What
    # is left is that there *is* one: an empty dict validates for any tool whose
    # arguments are all optional, so "it validated" is not "it exists".
    assert spec.example, f"{spec.name} publishes no example"
    assert "idempotency_key" in spec.example


@pytest.mark.parametrize("spec", ACTION_TOOLS, ids=lambda spec: spec.name)
def test_every_reference_in_an_example_is_minted_by_one_session(spec: ToolSpec) -> None:
    """A reference is session-scoped, so a mixed-session example cannot be replayed.

    ``tests/contract/test_mcp_action_coverage.py`` drives the adapters with
    these values under exactly one session id; a second one in here would be
    refused as INVALID_REF and the seam check would be testing the refusal.
    """
    for name, value in spec.example.items():
        if name in ENVELOPE or not isinstance(value, str):
            continue
        if ":" not in value:
            continue
        assert value.split(":")[1] == EXAMPLE_SESSION_ID, f"{spec.name}.{name}"


# -- the one that is gated on more than arming -----------------------------


def test_sleep_is_withheld_on_a_build_where_its_capability_is_only_experimental() -> None:
    """Sleep has no constructible timed action, so a clean scan reports experimental.

    That is not a formality: a sleeping character cannot be woken by this mod —
    there is no queue entry to clear — so a build that cannot say the entry
    point works is a build where this tool is not offered at all.
    """
    report = make_report(experimental=[SURVIVAL_SLEEP])

    assert "pz_action_sleep" not in {spec.name for spec in published_tools(report)}
    assert withheld_tools(report)["pz_action_sleep"] == "EXPERIMENTAL_API"


def test_sleep_is_the_only_p4_tool_and_says_why_in_its_own_description() -> None:
    p4 = {spec.name for spec in TOOLS if spec.risk is RiskClass.P4}
    summary = TOOLS_BY_NAME["pz_action_sleep"].summary.lower()

    assert p4 == {"pz_action_sleep"}
    assert "cannot wake" in summary
    assert "cancel" in summary
