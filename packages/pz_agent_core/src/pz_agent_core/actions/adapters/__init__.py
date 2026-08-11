"""The adapters that drive a real Build 42 API, one module per skill.

:mod:`pz_agent_core.actions.builtin` holds the actions that need no game API at
all. Most of what is here needs one, so most adapters in this package name a
``required_capability`` and the engine refuses to run them until a probe says
that capability is usable — an adapter that tried anyway would fail halfway
through, with the world already half-changed and nothing to roll back to.

The exceptions are the five read-only actions and the cancel. What a look, a
container listing, a search, a recipe reading and a square reading need is not a
Lua class but the observation tier they read;
:mod:`pz_agent_core.capabilities.probes` resolves Lua symbols, and a probe over
Java accessors it can never see would report ``unsupported`` on a healthy
install. They gate on the tier instead, which is a fact this side can check.
``plan.cancel`` names none for a different reason: a stop that a missing scan
could refuse is not a stop.

Each adapter answers exactly one question that cannot be faked: *what observable
change proves this happened?*

=============================  =========================================
``movement.move_to``           the position is inside the radius, on the floor
``movement.move_near``         the object is within reach, on its floor
``door.open``                  the door is observed open in the following snapshot
``door.close``                 the door is observed closed in the following snapshot
``door.unlock``                the lock is observed off in the following snapshot
``world.inspect``              the report names squares that were asked about
``container.inspect``          the report names the container that was asked about
``container.open_nearby``      the container is reported, accessible and in reach
``inventory.search``           every reference returned resolves on the character
``inventory.transfer``         the item is in the destination and nowhere else
``inventory.transfer_batch``   every named item is in the destination, each once
``inventory.ensure_main``      the item is in player-main
``consume.eat``                hunger fell, or the portions did
``consume.drink``              thirst fell, or the volume did
``consume.drink_source``       thirst fell after filling a vessel at a source
``literature.read``            the page counter advanced
``equipment.equip``            the requested slot holds the item
``equipment.unequip``          no slot holds it and it is still carried
``medical.bandage``            the dressed part is no longer bleeding
``survival.rest``              endurance reached the target, or the posture was taken
``survival.sleep``             fatigue fell *and* the world clock advanced
``plan.cancel``                nothing this session owns is still in flight
``combat.equip_best``          the primary hand changed to an observed weapon
``combat.shove``               the target re-reads down or further away
``combat.engage``              the target re-reads down, or is honestly gone
``combat.retreat``             the nearest observed zombie's distance grew
``crafting.inspect``           the readout answers for the recipe that was named
``crafting.craft``             one more of the recipe's product is in the inventory
``building.inspect``           the readout and the window answer for that square
``building.build``             the structure is observed standing on the square
=============================  =========================================

None of them selects anything. The item, the square, the book, the wound and the
dressing arrive already chosen by :mod:`pz_agent_core.policy`, which is where
the decision is deterministic and separately tested.
"""

from __future__ import annotations

from ...protocol import ActionName
from ..adapter import AdapterRegistry
from .building import BuildingBuildAdapter, BuildingInspectAdapter
from .combat import (
    CombatEngageAdapter,
    CombatEquipBestAdapter,
    CombatRetreatAdapter,
    CombatShoveAdapter,
)
from .common import (
    ContainerChain,
    ItemIdentity,
    Prerequisite,
)
from .consume import DrinkAdapter, DrinkSourceAdapter, EatAdapter, ensure_main_prerequisite
from .container import ContainerInspectAdapter, ContainerOpenNearbyAdapter
from .crafting import CraftingCraftAdapter, CraftingInspectAdapter
from .doors import DoorCloseAdapter, DoorOpenAdapter, DoorUnlockAdapter
from .equipment import EquipAdapter, UnequipAdapter
from .inventory import (
    BatchTransferAdapter,
    EnsureMainAdapter,
    SearchAdapter,
    TransferAdapter,
    unequip_prerequisite,
)
from .literature import ReadAdapter
from .medical import BandageAdapter
from .movement import MOVE_RETRY_POLICY, MoveNearAdapter, MoveToAdapter
from .plan import PlanCancelAdapter
from .survival import RestAdapter, SleepAdapter
from .world import WorldInspectAdapter

__all__ = [
    "MOVE_RETRY_POLICY",
    "BandageAdapter",
    "BatchTransferAdapter",
    "BuildingBuildAdapter",
    "BuildingInspectAdapter",
    "CombatEngageAdapter",
    "CombatEquipBestAdapter",
    "CombatRetreatAdapter",
    "CombatShoveAdapter",
    "ContainerChain",
    "ContainerInspectAdapter",
    "ContainerOpenNearbyAdapter",
    "CraftingCraftAdapter",
    "CraftingInspectAdapter",
    "DoorCloseAdapter",
    "DoorOpenAdapter",
    "DoorUnlockAdapter",
    "DrinkAdapter",
    "DrinkSourceAdapter",
    "EatAdapter",
    "EnsureMainAdapter",
    "EquipAdapter",
    "ItemIdentity",
    "MoveNearAdapter",
    "MoveToAdapter",
    "PlanCancelAdapter",
    "Prerequisite",
    "ReadAdapter",
    "RestAdapter",
    "SearchAdapter",
    "SleepAdapter",
    "TransferAdapter",
    "UnequipAdapter",
    "WorldInspectAdapter",
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
    registry.register(DoorOpenAdapter())
    registry.register(DoorCloseAdapter())
    registry.register(DoorUnlockAdapter())
    registry.register(WorldInspectAdapter())
    registry.register(ContainerInspectAdapter())
    registry.register(ContainerOpenNearbyAdapter())
    registry.register(SearchAdapter())
    registry.register(TransferAdapter())
    registry.register(BatchTransferAdapter())
    registry.register(EnsureMainAdapter())
    registry.register(EatAdapter())
    registry.register(DrinkAdapter())
    registry.register(DrinkSourceAdapter())
    registry.register(ReadAdapter())
    registry.register(EquipAdapter())
    registry.register(UnequipAdapter())
    registry.register(BandageAdapter())
    registry.register(RestAdapter())
    registry.register(SleepAdapter())
    # The assisted-combat rung, all four P4 behind the combat_assist
    # capability. Registered unconditionally like every other adapter: the
    # engine's capability check and the P4 permission gate are what keep them
    # from ever running unasked, and hiding them from the registry would hide
    # them from the census tests that pin exactly those properties.
    registry.register(CombatEquipBestAdapter())
    registry.register(CombatShoveAdapter())
    registry.register(CombatEngageAdapter())
    registry.register(CombatRetreatAdapter())
    # The crafting rung. The reading is P0 and gates on the observation tier;
    # only the craft rides the ``crafting`` capability, which starts
    # experimental and is withheld until a live run promotes it. The craft's
    # base tier is P3 — spending materials is irreversible even when nothing
    # moves — and it escalates itself to P4 per command when the recipe needs
    # a surface or a world container, which is a fact about the arguments and
    # therefore never visible from the action name.
    registry.register(CraftingInspectAdapter())
    registry.register(CraftingCraftAdapter())
    # The building rung. The reading is P0 and gates on the observation tiers
    # it reads, like the other four reads and like the mod's own declaration;
    # only the placement rides the ``building`` capability, which starts
    # experimental and is withheld until a live run promotes it. The build's
    # tier is P4 and it is flat: there is no ``risk_for`` to escalate through
    # because there is nothing above P4 and no version of placing a permanent
    # object that is worth less than the top of the ladder. Nothing in this
    # wave takes a structure back down, either, which is why the policy behind
    # these two refuses far more readily than the crafting policy does.
    registry.register(BuildingInspectAdapter())
    registry.register(BuildingBuildAdapter())
    # The one deference in the file, and the only registration here that is
    # conditional. ``register_builtins`` publishes an API-free cancel, and a
    # session that has both must keep that one: stopping has to work before
    # anything has been probed, so the stricter session-scoped cancel takes the
    # action only when nothing else has claimed it. This never *replaces* an
    # adapter — single assignment still holds — it declines to.
    if ActionName.PLAN_CANCEL not in registry:
        registry.register(PlanCancelAdapter())
    return registry
