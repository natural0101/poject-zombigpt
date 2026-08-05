"""What the adapter package as a whole promises the engine.

Two properties are checked here rather than per adapter, because they are only
true of the set: every game action has exactly one adapter, and every one of
them is gated on a capability that a real probe declares. A capability name
invented in an adapter would gate on something no probe can ever satisfy, and
the action would look implemented while being permanently unavailable.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import AdapterRegistry, DuplicateAdapterError, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.adapters.common import MAX_PREREQUISITES, Prerequisite, refused
from pz_agent_core.capabilities import PROBES_BY_NAME
from pz_agent_core.protocol import ActionName, ReasonCode, RiskClass

#: The actions this package implements. ``session.*``, ``safety.stop``,
#: ``world.inspect``, ``inventory.equip``/``unequip`` are deliberately absent.
IMPLEMENTED = {
    ActionName.MOVEMENT_MOVE_TO,
    ActionName.MOVEMENT_MOVE_NEAR,
    ActionName.INVENTORY_TRANSFER,
    ActionName.INVENTORY_ENSURE_MAIN,
    ActionName.CONSUME_EAT,
    ActionName.CONSUME_DRINK,
    ActionName.LITERATURE_READ,
}


def test_every_game_action_gets_exactly_one_adapter() -> None:
    registry = register_game_adapters(AdapterRegistry())

    assert set(registry.names()) == IMPLEMENTED


def test_registering_twice_is_refused_rather_than_silently_replacing() -> None:
    registry = register_game_adapters(AdapterRegistry())

    with pytest.raises(DuplicateAdapterError):
        register_game_adapters(registry)


def test_the_game_adapters_coexist_with_the_api_free_ones() -> None:
    registry = register_game_adapters(register_builtins(AdapterRegistry()))

    assert len(registry) == len(IMPLEMENTED) + 2


def test_every_adapter_names_a_capability_a_probe_actually_declares() -> None:
    registry = register_game_adapters(AdapterRegistry())

    for action in registry.names():
        capability = registry.get(action).required_capability
        assert capability is not None, action
        assert capability in PROBES_BY_NAME, capability


def test_no_adapter_here_is_free_of_a_permission_tier() -> None:
    """Everything in this package changes the world, so nothing is P0."""
    registry = register_game_adapters(AdapterRegistry())

    for action in registry.names():
        assert registry.get(action).risk is not RiskClass.P0


def test_every_adapter_bounds_its_own_polling() -> None:
    registry = register_game_adapters(AdapterRegistry())

    for action in registry.names():
        adapter = registry.get(action)
        assert adapter.poll_interval_ms > 0
        assert adapter.timeout_ms >= adapter.poll_interval_ms


def test_a_refusal_reports_a_bounded_number_of_prerequisites() -> None:
    many = [
        Prerequisite(action=ActionName.INVENTORY_ENSURE_MAIN, args={"n": n}, detail=f"step {n}")
        for n in range(MAX_PREREQUISITES + 5)
    ]

    failure = refused(
        "too much preparation", reason_code=ReasonCode.PRECONDITION_FAILED, prerequisites=many
    )

    assert len(failure.evidence["prerequisites"]) == MAX_PREREQUISITES


def test_a_prerequisite_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="explain"):
        Prerequisite(action=ActionName.INVENTORY_ENSURE_MAIN, args={}, detail="  ")


def test_a_refusal_without_prerequisites_carries_none() -> None:
    failure = refused("nothing to prepare", reason_code=ReasonCode.INVALID_REF)

    assert "prerequisites" not in failure.evidence
