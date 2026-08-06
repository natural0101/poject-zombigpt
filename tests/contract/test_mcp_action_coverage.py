"""The seam between the action engine and the tool surface.

Both halves of this project have been complete, tested and green while not being
connected to each other — three times. An adapter suite passes by exercising
adapters; a catalogue suite passes by exercising the catalogue; neither notices
that the catalogue publishes six of the seventeen actions the engine can run,
because nothing in either suite ever looks at the other side.

So this file asserts the *connection*, and nothing else:

1. The two sets are one set. Every action with a registered adapter is reachable
   through exactly one MCP tool, and every tool that names an action names one
   the engine can actually run. An orphan in either direction is a feature that
   exists and cannot be used, or a tool that is advertised and cannot work.
2. Every tool in the catalogue has a handler in the router. The router's
   constructor already refuses to exist without one; what is added here is the
   other direction, so a handler left behind by a removed tool is visible too.
3. The read/write split agrees with the protocol's
   :data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS`. This is the check with a
   safety consequence: a tool on the wrong side of it moves the character on a
   disarmed session.
4. The arguments a tool declares are arguments its adapter accepts, driven from
   the tool's own published example. The adapter may refuse the example for any
   domain reason — an item that is not in the inventory, a square that is not
   loaded — but never because it was handed an argument it does not know, a
   reference of the wrong kind, or a number outside its range. All three of
   those are ``INVALID_ARGUMENT`` or a reference refusal, and both are refusals
   the caller could not have avoided.

Point 4 is the one that would have caught the two bugs this seam has already
produced: a payload naming ``origin``, which no adapter declared, and a
``move_near`` insisting on an ``object:`` reference the observer never mints.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from pz_agent_core.actions import ActionAdapter, AdapterRegistry, register_builtins
from pz_agent_core.actions.adapter import PreconditionFailed
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    READ_ONLY_ACTIONS,
    ActionName,
    Observation,
    ReasonCode,
)
from pz_agent_mcp.catalog import EXAMPLE_SESSION_ID, TOOLS, TOOLS_BY_NAME, ToolSpec
from pz_agent_mcp.router import ToolRouter
from pz_agent_mcp.validation import validate_arguments
from tests.fixtures import make_observation
from tests.fixtures.ipc_builders import make_command
from tests.fixtures.mcp_doubles import Doubles, make_report

pytestmark = pytest.mark.contract

#: Fields of the command envelope, which never reach an adapter.
ENVELOPE: Final[frozenset[str]] = frozenset({"idempotency_key", "timeout_ms"})

#: Actions a session publishes but no tool names, with the reason. Arming and
#: disarming are session lifecycle rather than commands the engine runs — they
#: have no adapter — so they are not in this list either; what would go here is
#: an action with a real adapter that this surface deliberately does not offer.
UNPUBLISHED_ACTIONS: Final[dict[ActionName, str]] = {}

#: Actions whose argument check cannot be driven from a published example, with
#: the reason it cannot. Empty, and the test below is what keeps it honest: an
#: entry added here is a hole in check (4), and a hole nobody can see is the
#: same bug in a new place.
EXEMPT_FROM_ARGUMENT_CHECK: Final[dict[ActionName, str]] = {}


def a_registry() -> AdapterRegistry:
    """The adapters a real session registers: the API-free two and the game set.

    The game set alone would report ``action.wait`` as an orphan tool, which
    would be a gap in this fixture rather than one in the surface.
    """
    return register_game_adapters(register_builtins(AdapterRegistry()))


def a_report_where_every_capability_is_usable() -> CapabilityReport:
    """So a call is judged on its routing rather than on this build's probes."""
    return make_report(
        usable=sorted(
            {spec.required_capability for spec in TOOLS if spec.required_capability is not None}
        )
    )


def action_tools() -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOLS if spec.action is not None)


def an_observation() -> Observation:
    """A live-looking world minted by the session the examples name.

    References carry the session that minted them and every adapter refuses one
    from another session, so the command and the world have to agree on which
    session this is before any argument can be judged on its merits.
    """
    return make_observation(session_id=EXAMPLE_SESSION_ID)


# -- (1) the two sets are one set ------------------------------------------


def test_every_action_with_an_adapter_is_reachable_through_a_tool() -> None:
    registered = set(a_registry().names())
    published = {spec.action for spec in action_tools()}

    orphans = registered - published - set(UNPUBLISHED_ACTIONS)

    assert not orphans, (
        "these actions have an adapter, evidence and tests, and no way for a "
        f"client to ask for them: {sorted(action.value for action in orphans)}"
    )


def test_every_tool_that_names_an_action_names_one_the_engine_can_run() -> None:
    registered = set(a_registry().names())

    unrunnable = {
        spec.name: spec.action for spec in action_tools() if spec.action not in registered
    }

    assert unrunnable == {}, "advertised tools whose action no adapter implements"


def test_no_action_is_offered_under_two_names() -> None:
    """Two tools for one action are two contracts for one postcondition.

    They would drift — different bounds, different defaults — and the caller has
    no way to tell which of the two the engine will hold them to.
    """
    seen: dict[ActionName, list[str]] = {}
    for spec in action_tools():
        assert spec.action is not None
        seen.setdefault(spec.action, []).append(spec.name)

    duplicated = {action.value: names for action, names in seen.items() if len(names) > 1}

    assert duplicated == {}


def test_an_unpublished_action_is_named_with_its_reason() -> None:
    for action, reason in UNPUBLISHED_ACTIONS.items():
        assert reason.strip(), f"{action.value} is unpublished without a reason"


# -- (2) every tool has a handler ------------------------------------------


def test_the_catalogue_and_the_routers_handlers_are_the_same_set() -> None:
    """The router refuses to start without a handler per tool; this adds the converse.

    Read through the private map on purpose: the constructor's own check proves
    one direction and there is no public view of the other, and a handler left
    behind by a removed tool is dead code that reads like a live route.
    """
    router = ToolRouter(Doubles().services)

    assert set(router._handlers) == set(TOOLS_BY_NAME)


@pytest.mark.parametrize("spec", action_tools(), ids=lambda spec: spec.name)
def test_every_action_tool_reaches_the_action_port_with_the_action_it_names(
    spec: ToolSpec,
) -> None:
    """A handler that exists is not yet a handler that submits the right thing."""
    doubles = Doubles()
    doubles.capabilities.report_value = a_report_where_every_capability_is_usable()
    router = ToolRouter(doubles.services)

    payload = router.call(spec.name, spec.example)

    assert payload["ok"] is True, payload
    assert doubles.actions.submitted[-1].action is spec.action
    assert (
        set(doubles.actions.submitted[-1].args) <= set(spec.input_schema["properties"]) - ENVELOPE
    )


# -- (3) the read-only classification --------------------------------------


@pytest.mark.parametrize("spec", action_tools(), ids=lambda spec: spec.name)
def test_a_tool_that_needs_no_arming_submits_an_action_that_is_allowed_unarmed(
    spec: ToolSpec,
) -> None:
    """The direction that matters: nothing skips the gate that should not.

    The converse is deliberately not asserted. ``action.wait`` is a read-only
    action published as a write tool, because holding still for a stretch of
    game time is a thing the agent *does* even though it reads nothing — being
    stricter than the protocol requires is always allowed here, being looser
    never is.
    """
    if spec.requires_armed:
        return

    assert spec.action in READ_ONLY_ACTIONS | ALWAYS_ALLOWED_ACTIONS, (
        f"{spec.name} skips the arming gate for {spec.action}, "
        "which the protocol does not permit unarmed"
    )


def test_every_action_outside_both_allowances_needs_an_armed_session() -> None:
    gated = {
        spec.name
        for spec in action_tools()
        if spec.action not in READ_ONLY_ACTIONS | ALWAYS_ALLOWED_ACTIONS
    }

    assert all(TOOLS_BY_NAME[name].requires_armed for name in gated), gated


def test_the_read_only_actions_the_surface_publishes_are_the_protocols_own() -> None:
    unarmed = {spec.action for spec in action_tools() if not spec.requires_armed}

    # container.open_nearby is the one whose name invites the mistake.
    assert ActionName.CONTAINER_OPEN_NEARBY not in unarmed
    assert unarmed <= READ_ONLY_ACTIONS | ALWAYS_ALLOWED_ACTIONS


# -- (4) the arguments an adapter will actually accept ---------------------


def adapter_arguments(spec: ToolSpec) -> dict[str, Any]:
    """The example, validated and defaulted, as the adapter would receive it."""
    validated = validate_arguments(spec.input_schema, spec.example)
    return {name: value for name, value in validated.items() if name not in ENVELOPE}


@pytest.mark.parametrize("spec", action_tools(), ids=lambda spec: spec.name)
def test_an_adapter_never_refuses_a_published_example_for_its_arguments(spec: ToolSpec) -> None:
    """Drive the adapter's own check with what a client would paste.

    ``INVALID_ARGUMENT`` is the code every argument reader in
    :mod:`pz_agent_core.actions.adapters.common` raises — an undeclared name, a
    number outside the range, a flag that is not a boolean — and
    ``INVALID_REF`` is what ``read_ref`` raises for a reference of the wrong
    kind. Neither is something the caller could have avoided by reading the
    schema, which makes both a disagreement between the two halves rather than a
    refusal of this particular world.

    Any other refusal is fine and expected: the observation below is an empty
    world, so most of these examples fail on a reference that resolves to
    nothing, which is the adapter doing its job.
    """
    assert spec.action is not None
    if spec.action in EXEMPT_FROM_ARGUMENT_CHECK:
        pytest.skip(EXEMPT_FROM_ARGUMENT_CHECK[spec.action])
    adapter: ActionAdapter = a_registry().get(spec.action)
    command = make_command(spec.action, session_id=EXAMPLE_SESSION_ID, args=adapter_arguments(spec))

    try:
        adapter.validate(command, an_observation())
    except PreconditionFailed as refusal:
        assert refusal.reason_code is not ReasonCode.INVALID_ARGUMENT, (
            f"{spec.name} publishes an argument its adapter refuses: {refusal}"
        )
        assert "reference" not in str(refusal), (
            f"{spec.name} publishes a reference its adapter refuses: {refusal}"
        )


def test_no_action_is_exempt_from_the_argument_check() -> None:
    """A named gap can be argued about; an unnamed one is invisible.

    Every adapter can be driven from its tool's example today. The assertion is
    what makes that a property rather than a coincidence: adding an exemption
    means editing this list, and editing it fails here until the entry is
    argued for in the reason and this expectation is changed deliberately.
    """
    for action, reason in EXEMPT_FROM_ARGUMENT_CHECK.items():
        assert reason.strip(), f"{action.value} is exempt without a reason"

    assert EXEMPT_FROM_ARGUMENT_CHECK == {}, (
        "an action was exempted from the argument check: "
        f"{sorted(action.value for action in EXEMPT_FROM_ARGUMENT_CHECK)}"
    )


def test_the_check_above_covers_every_action_the_engine_can_run() -> None:
    """Guards the parametrisation itself, which is where coverage quietly leaks.

    The cases come from the catalogue, so an action with an adapter and no tool
    would simply not be parametrised — and the check would report nothing wrong
    while covering less.
    """
    checked = {spec.action for spec in action_tools()} - set(EXEMPT_FROM_ARGUMENT_CHECK)

    assert checked == set(a_registry().names()) - set(UNPUBLISHED_ACTIONS)
