"""What the adapter package as a whole promises the engine.

These properties are checked here rather than per adapter, because they are only
true of the set: every game action has exactly one adapter; every adapter that
needs a game API is gated on a capability a real probe declares; and the ones
that need no API are exactly the read-only actions plus the cancel. A capability
name invented in an adapter would gate on something no probe can satisfy, and
the action would look implemented while being permanently unavailable — while a
*missing* capability on a mutating adapter is the opposite failure, an ungated
write path.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import AdapterRegistry, DuplicateAdapterError, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.adapters.common import MAX_PREREQUISITES, Prerequisite, refused
from pz_agent_core.capabilities import PROBES_BY_NAME
from pz_agent_core.protocol import READ_ONLY_ACTIONS, ActionName, ReasonCode, RiskClass

#: The actions this package implements. ``session.*``, ``safety.stop`` and
#: ``action.wait`` are deliberately absent: the first two are handled by the mod
#: directly and the third needs no game API at all.
IMPLEMENTED = {
    ActionName.MOVEMENT_MOVE_TO,
    ActionName.MOVEMENT_MOVE_NEAR,
    ActionName.DOOR_OPEN,
    ActionName.DOOR_CLOSE,
    ActionName.DOOR_UNLOCK,
    ActionName.WORLD_INSPECT,
    ActionName.CONTAINER_INSPECT,
    ActionName.CONTAINER_OPEN_NEARBY,
    ActionName.INVENTORY_SEARCH,
    ActionName.INVENTORY_TRANSFER,
    ActionName.INVENTORY_ENSURE_MAIN,
    ActionName.CONSUME_EAT,
    ActionName.CONSUME_DRINK,
    ActionName.CONSUME_DRINK_SOURCE,
    ActionName.LITERATURE_READ,
    ActionName.EQUIPMENT_EQUIP,
    ActionName.EQUIPMENT_UNEQUIP,
    ActionName.MEDICAL_BANDAGE,
    ActionName.SURVIVAL_REST,
    ActionName.SURVIVAL_SLEEP,
    ActionName.PLAN_CANCEL,
}

#: The actions here that only read. They are the ones permitted in OBSERVE mode,
#: and the ones whose gate is an observation tier rather than a probe.
READ_ONLY_HERE = IMPLEMENTED & READ_ONLY_ACTIONS

#: Actions in this package that name no capability, and why each is allowed to:
#: a reading needs the observation tier it reads, and a stop must not be
#: refusable because a scan has not run.
CAPABILITY_FREE = READ_ONLY_HERE | {ActionName.PLAN_CANCEL}


def test_every_game_action_gets_exactly_one_adapter() -> None:
    registry = register_game_adapters(AdapterRegistry())

    assert set(registry.names()) == IMPLEMENTED


def test_registering_twice_is_refused_rather_than_silently_replacing() -> None:
    registry = register_game_adapters(AdapterRegistry())

    with pytest.raises(DuplicateAdapterError):
        register_game_adapters(registry)


def test_the_game_adapters_coexist_with_the_api_free_ones() -> None:
    """``plan.cancel`` is the one action both halves implement, so it is counted once."""
    registry = register_game_adapters(register_builtins(AdapterRegistry()))

    assert len(registry) == len(IMPLEMENTED) + 1
    assert set(registry.names()) == IMPLEMENTED | {ActionName.ACTION_WAIT}


def test_every_mutating_adapter_names_a_capability_a_probe_actually_declares() -> None:
    registry = register_game_adapters(AdapterRegistry())

    for action in registry.names():
        if action in CAPABILITY_FREE:
            continue
        capability = registry.get(action).required_capability
        assert capability is not None, action
        assert capability in PROBES_BY_NAME, capability


def test_the_only_adapters_without_a_capability_are_the_reads_and_the_stop() -> None:
    """A probe over accessors the Lua scan cannot see would gate on a false negative."""
    registry = register_game_adapters(AdapterRegistry())

    free = {a for a in registry.names() if registry.get(a).required_capability is None}
    assert free == CAPABILITY_FREE


def test_only_the_read_only_actions_are_free_of_a_permission_tier() -> None:
    """Everything else here changes the world, so nothing else may sit at P0."""
    registry = register_game_adapters(AdapterRegistry())

    for action in registry.names():
        risk = registry.get(action).risk
        assert (risk is RiskClass.P0) == (action in READ_ONLY_HERE), action


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
