"""Reusable payload builders for tests.

Hand-writing a full observation in every test buries the one field under test
in eighty lines of scaffolding. These builders produce a valid baseline and let
each test override only what it is actually about.
"""

from __future__ import annotations

import uuid
from typing import Any

from pz_agent_core.protocol import (
    ActionOwnership,
    ActionState,
    ContainerKind,
    ContainerView,
    DangerLevel,
    GameState,
    Hands,
    InventoryView,
    ItemView,
    NearbyView,
    Observation,
    PlayerState,
    Position,
    SafetyState,
    SessionMode,
)

DEFAULT_SESSION = str(uuid.UUID(int=0x5E5510))
DEFAULT_SAVE = "save-8f3a2c1d"


def make_game(**overrides: Any) -> GameState:
    base: dict[str, Any] = {
        "build": "42.20",
        "save_id": DEFAULT_SAVE,
        "paused": False,
        "speed": 1.0,
        "world_time": "1993-07-09T14:20:00",
    }
    base.update(overrides)
    return GameState(**base)


def make_player(**overrides: Any) -> PlayerState:
    stats = {
        "health": 1.0,
        "endurance": 0.9,
        "hunger": 0.2,
        "thirst": 0.2,
        "fatigue": 0.1,
    }
    stats.update(overrides.pop("stats", {}))
    base: dict[str, Any] = {
        "present": True,
        "alive": True,
        "position": Position(x=1200.0, y=3400.0, z=0, direction="S"),
        "stats": stats,
        "moodles": {},
        "wounds": [],
        "hands": Hands(),
    }
    base.update(overrides)
    return PlayerState(**base)


def make_safety(**overrides: Any) -> SafetyState:
    base: dict[str, Any] = {
        "armed": True,
        "mode": SessionMode.ASSISTED,
        "danger_level": DangerLevel.NONE,
        "manual_takeover": False,
        "sidecar_stale": False,
    }
    base.update(overrides)
    return SafetyState(**base)


def make_action_state(**overrides: Any) -> ActionState:
    base: dict[str, Any] = {"ownership": ActionOwnership.NONE, "busy": False}
    base.update(overrides)
    return ActionState(**base)


def make_container(
    ref: str,
    kind: ContainerKind = ContainerKind.CARRIED,
    **overrides: Any,
) -> ContainerView:
    base: dict[str, Any] = {
        "ref": ref,
        "kind": kind,
        "name": ref.rsplit(":", 1)[-1],
        "parent_ref": None,
        "capacity": 20.0,
        "used_capacity": 3.0,
        "accessible": True,
    }
    base.update(overrides)
    return ContainerView(**base)


def make_item(ref: str, container_ref: str, **overrides: Any) -> ItemView:
    base: dict[str, Any] = {
        "ref": ref,
        "container_ref": container_ref,
        "full_type": "Base.TinnedBeans",
        "display_name": "Tinned Beans",
        "category": "Food",
        "weight": 0.8,
    }
    base.update(overrides)
    return ItemView(**base)


def make_observation(**overrides: Any) -> Observation:
    base: dict[str, Any] = {
        "session_id": DEFAULT_SESSION,
        "seq": 1,
        "timestamp_ms": 1_700_000_000_000,
        "game": make_game(),
        "player": make_player(),
        "safety": make_safety(),
        "action": make_action_state(),
        "full": True,
        "inventory": InventoryView(),
        "nearby": NearbyView(),
        "capability_revision": 1,
    }
    base.update(overrides)
    return Observation(**base)


def main_container_ref(session_id: str = DEFAULT_SESSION) -> str:
    return f"container:{session_id}:player-main"


def backpack_container_ref(session_id: str = DEFAULT_SESSION) -> str:
    return f"container:{session_id}:worn:Back:99001"


def item_ref(runtime_id: str, container_tail: str, session_id: str = DEFAULT_SESSION) -> str:
    return f"item:{session_id}:{container_tail}:{runtime_id}:0"
