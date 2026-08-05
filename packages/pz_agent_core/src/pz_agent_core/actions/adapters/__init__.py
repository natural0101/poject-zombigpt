"""The adapters that drive a real Build 42 API, one module per skill.

:mod:`pz_agent_core.actions.builtin` holds the two actions that need no game
API at all. Everything here needs one, so every adapter in this package names a
``required_capability`` and the engine refuses to run it until a probe says that
capability is usable — an adapter that tried anyway would fail halfway through,
with the world already half-changed and nothing to roll back to.

Each adapter answers exactly one question that cannot be faked: *what observable
change proves this happened?*

============================  ==========================================
``movement.move_to``          the position is inside the radius, on the floor
``movement.move_near``        the object is within reach, on its floor
``inventory.transfer``        the item is in the destination and nowhere else
``inventory.ensure_main``     the item is in player-main
``consume.eat``               hunger fell, or the portions did
``consume.drink``             thirst fell, or the volume did
``literature.read``           the page counter advanced
============================  ==========================================

None of them selects anything. The item, the square and the book arrive already
chosen by :mod:`pz_agent_core.policy`, which is where the decision is
deterministic and separately tested.
"""

from __future__ import annotations

from ..adapter import AdapterRegistry
from .common import (
    ContainerChain,
    ItemIdentity,
    Prerequisite,
)
from .consume import DrinkAdapter, EatAdapter, ensure_main_prerequisite
from .inventory import EnsureMainAdapter, TransferAdapter, unequip_prerequisite
from .literature import ReadAdapter
from .movement import MOVE_RETRY_POLICY, MoveNearAdapter, MoveToAdapter

__all__ = [
    "MOVE_RETRY_POLICY",
    "ContainerChain",
    "DrinkAdapter",
    "EatAdapter",
    "EnsureMainAdapter",
    "ItemIdentity",
    "MoveNearAdapter",
    "MoveToAdapter",
    "Prerequisite",
    "ReadAdapter",
    "TransferAdapter",
    "ensure_main_prerequisite",
    "register_game_adapters",
    "unequip_prerequisite",
]


def register_game_adapters(registry: AdapterRegistry) -> AdapterRegistry:
    """Add every game-API adapter to *registry* and return it.

    A function rather than a module-level singleton for the same reason
    ``register_builtins`` is one: a session decides what it publishes, and
    registration is single-assignment, so a registry built twice by accident
    raises instead of quietly swapping out the code that decides what counts as
    proof.
    """
    registry.register(MoveToAdapter())
    registry.register(MoveNearAdapter())
    registry.register(TransferAdapter())
    registry.register(EnsureMainAdapter())
    registry.register(EatAdapter())
    registry.register(DrinkAdapter())
    registry.register(ReadAdapter())
    return registry
