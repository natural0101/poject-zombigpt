"""Both halves of the wire must name the same capability for the same action.

The sidecar gates an action on ``adapter.required_capability``. The mod
publishes its own capability document keyed on ``adapter.capability``, which is
what a diagnostic reads to answer "why was that refused". Nothing compared the
two, and they had drifted: five Lua adapters declared ``capability = nil`` with
comments asserting that no probe existed for them —

    equipment.equip   "No probe declares an equip capability"
    equipment.unequip "No probe declares an equip capability"
    medical.bandage   "No probe in pz_agent_core.capabilities declares a bandage
                       capability yet"
    survival.rest     "No probe declares a rest capability"
    survival.sleep    "No probe declares a sleep capability, and §12.4 does not
                       list one"

— while ``probes.PROBES`` declares all five, each with required symbols, a
confirmation naming the matching action, and in ``survival_sleep``'s case an
``experimental`` ceiling that exists for a specific safety reason: once the
character is asleep there is no timed action to interrupt and no queue entry to
cancel, so a panic stop cannot reach them.

The action gate was never open — the mod enforces by required symbols and the
sidecar enforces by the ledger — so this cost no command. What it cost was the
mod's capability document, which named six capabilities where the system knows
twelve, so five of them were simply absent from the report a person consults
when something is refused. And it cost five comments telling the next reader a
falsehood about the code beside them.

Three actions declare no capability on either side, and that is correct:
``world.inspect``, ``container.inspect`` and ``inventory.search`` only read, and
everything they read is reached through Java accessors that never appear in the
game's Lua. A probe over those names would report ``unsupported`` on a perfectly
healthy install, so they gate on the observation tier they need instead. This
file asserts that exemption is exactly three, by name, so a fourth cannot join
it quietly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.actions import AdapterRegistry, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.capabilities.probes import PROBES
from pz_agent_core.protocol import ActionName

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_adapter_capabilities.lua"

_INTERPRETERS: Final = ("lua5.4", "lua")

#: The three read-only actions, gated on an observation tier rather than a
#: probe. Listed rather than derived, so that adding a fourth has to be done
#: here, by someone who then has to justify it.
NO_PROBE_BY_DESIGN: Final = frozenset(
    {
        ActionName.WORLD_INSPECT,
        ActionName.CONTAINER_INSPECT,
        ActionName.INVENTORY_SEARCH,
    }
)


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip("no Lua interpreter is installed; the mod's own suite needs one too")


@pytest.fixture(scope="module")
def lua_capabilities() -> dict[str, str | None]:
    """What each shipped Lua adapter declares, read from the real load path."""
    assert DUMPER.is_file(), f"missing dumper: {DUMPER}"
    completed = subprocess.run(
        [_interpreter(), str(DUMPER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    # The dumper writes `false` for "declares none", because a nil would drop
    # the key and make that indistinguishable from an adapter that did not load.
    declared = {action: (value or None) for action, value in document.items()}
    assert declared, "the dumper produced no adapters, so this check would prove nothing"
    return declared


@pytest.fixture(scope="module")
def python_capabilities() -> dict[str, str | None]:
    registry = register_game_adapters(register_builtins(AdapterRegistry()))
    return {action.value: registry.get(action).required_capability for action in registry.names()}


def test_the_two_sides_name_the_same_capability_for_every_shared_action(
    lua_capabilities: dict[str, str | None], python_capabilities: dict[str, str | None]
) -> None:
    """The assertion that would have caught the drift.

    Only actions both sides implement are compared: the control plane has no Lua
    adapter and ``plan.cancel`` is a Python builtin, so neither is a disagreement.
    """
    shared = sorted(set(lua_capabilities) & set(python_capabilities))
    assert len(shared) >= 15, f"only {len(shared)} shared actions; the comparison has gone hollow"

    disagreements = {
        action: {"lua": lua_capabilities[action], "python": python_capabilities[action]}
        for action in shared
        if lua_capabilities[action] != python_capabilities[action]
    }
    assert disagreements == {}, (
        "the mod and the sidecar name different capabilities for these actions; "
        "the sidecar's is what gates, the mod's is what a diagnostic reads"
    )


def test_every_capability_either_side_names_is_a_probe_that_exists(
    lua_capabilities: dict[str, str | None], python_capabilities: dict[str, str | None]
) -> None:
    """A capability string nobody declares gates on nothing at all."""
    declared = {probe.capability for probe in PROBES}
    named = {value for value in lua_capabilities.values() if value is not None}
    named |= {value for value in python_capabilities.values() if value is not None}

    assert named <= declared, f"named but not declared by any probe: {sorted(named - declared)}"


def test_an_action_without_a_capability_is_one_of_the_three_that_should_have_none(
    lua_capabilities: dict[str, str | None], python_capabilities: dict[str, str | None]
) -> None:
    """The exemption is a closed list, not a habit.

    Five actions were in this state and none of them belonged: probes existed
    for all five, and the comments beside them said otherwise.
    """
    ungated_lua = {action for action, capability in lua_capabilities.items() if capability is None}
    ungated_python = {
        action
        for action, capability in python_capabilities.items()
        if capability is None and action in lua_capabilities
    }
    expected = {action.value for action in NO_PROBE_BY_DESIGN}

    assert ungated_lua == expected, "the mod leaves these actions without a capability"
    assert ungated_python == expected, "the sidecar leaves these actions without a capability"


def test_every_probe_with_a_confirming_action_is_claimed_by_that_action() -> None:
    """A probe whose action names a different capability can never be confirmed.

    ``confirm()`` matches an ack against ``probe.confirmation.action``; if the
    adapter for that action gates on some other capability, the ack that would
    promote this probe is one the engine would never let through.
    """
    registry = register_game_adapters(register_builtins(AdapterRegistry()))
    unreachable: dict[str, str] = {}
    for probe in PROBES:
        if not probe.can_be_verified:
            continue
        action = probe.confirmation.action
        if action not in registry:
            unreachable[probe.capability] = f"no adapter for {action.value}"
            continue
        gates_on = registry.get(action).required_capability
        if gates_on != probe.capability:
            unreachable[probe.capability] = (
                f"{action.value} gates on {gates_on!r}, so this probe's own "
                "confirmation could never arrive"
            )

    assert unreachable == {}
