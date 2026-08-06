"""Builders for the planner, critic and executor tests (T023).

A planner test is about one thing: this field cannot be constructed, that step
is over the cap, this need outranks the plan. Everything else — a registry with
nine adapters, a capability snapshot, an inventory holding one edible tin — is
scaffolding, so it lives here.

The defaults describe a session in which a plan *would* be approved: armed,
``ASSISTED``, calm, every capability verified, the tin in the main inventory.
That matters more than usual here, because a refusal test whose default session
was already refusing would pass for the wrong reason.
"""

from __future__ import annotations

import uuid
from typing import Any

from pz_agent_core.actions import AdapterRegistry, register_builtins
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.capabilities import (
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)
from pz_agent_core.planner import (
    ConsumeArgs,
    Goal,
    GoalKind,
    Plan,
    PlanProposal,
    PlanRequest,
    PlanStep,
    SuccessCriterion,
    SuccessKind,
)
from pz_agent_core.policy.config import CAPABILITY_DRINK_PERCENTAGE
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import ActionName, InventoryView, Observation
from tests.fixtures import DEFAULT_SESSION, make_observation
from tests.fixtures.policy_items import (
    MAIN_REF,
    drink_item,
    food_item,
    inventory,
    literature_item,
    policy_item_ref,
)

GOAL_ID = str(uuid.UUID(int=0x60A1))
OTHER_SESSION = str(uuid.UUID(int=0xA11E))

#: Every probe the adapters and the selection policies name, all verified. A
#: capability test switches one off; nothing else has to think about them.
#: ``drink_percentage`` is here because without it the drink policy refuses to
#: empty a lone bottle below critical thirst, which would make every drink test
#: fail for a reason that has nothing to do with planning.
ALL_CAPABILITIES = CapabilitySnapshot.verified(
    EAT_PERCENTAGE,
    DRINK_CARRIED,
    CAPABILITY_DRINK_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)

NO_PROBES = CapabilitySnapshot()


def goal(kind: GoalKind = GoalKind.SATISFY_HUNGER, **overrides: Any) -> Goal:
    base: dict[str, Any] = {"goal_id": GOAL_ID, "kind": kind}
    base.update(overrides)
    return Goal(**base)


def full_registry() -> AdapterRegistry:
    """Every adapter this build ships, builtins included."""
    return register_game_adapters(register_builtins(AdapterRegistry()))


def stocked_inventory(**overrides: Any) -> InventoryView:
    """One tin, one bottle and one paperback, all in the main inventory."""
    items = overrides.pop(
        "items",
        [
            food_item("beans"),
            drink_item("bottle"),
            literature_item("paperback"),
        ],
    )
    return inventory(*items, **overrides)


def planner_observation(**overrides: Any) -> Observation:
    """A calm armed session with something to eat, drink and read."""
    overrides.setdefault("inventory", stocked_inventory())
    return make_observation(**overrides)


def item_ref(runtime_id: str, container_ref: str = MAIN_REF) -> str:
    return policy_item_ref(runtime_id, container_ref)


def foreign_item_ref(runtime_id: str = "beans") -> str:
    """An item reference minted by a session that is not this one."""
    return f"item:{OTHER_SESSION}:player-main:{runtime_id}:0"


def eat_step(step_id: str = "s1", **overrides: Any) -> PlanStep:
    base: dict[str, Any] = {
        "step_id": step_id,
        "action": ActionName.CONSUME_EAT,
        "args": ConsumeArgs(item_ref=item_ref("beans"), fraction=1.0),
        "success": SuccessCriterion(kind=SuccessKind.HUNGER_AT_MOST, value=0.15),
    }
    base.update(overrides)
    return PlanStep(**base)


def eat_plan(*steps: PlanStep, **overrides: Any) -> Plan:
    base: dict[str, Any] = {
        "goal_id": GOAL_ID,
        "summary": "Eat the beans",
        "steps": steps or (eat_step(),),
    }
    base.update(overrides)
    return Plan(**base)


def plan_payload(**overrides: Any) -> dict[str, Any]:
    """The wire form of a minimal valid plan, for the strict-parsing tests."""
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "goal_id": GOAL_ID,
        "summary": "Eat the beans",
        "risk_class": "P2",
        "steps": [step_payload()],
    }
    base.update(overrides)
    return base


def step_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "step_id": "s1",
        "action": "consume.eat",
        "args": {"item_ref": item_ref("beans"), "fraction": 1.0},
        "success": {"type": "hunger_at_most", "value": 0.15},
        "on_failure": "ask_user",
        "requires_confirmation": False,
    }
    base.update(overrides)
    return base


def plan_request(observation: Observation | None = None, **overrides: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "goal": goal(),
        "observation": observation if observation is not None else planner_observation(),
        "capabilities": ALL_CAPABILITIES,
    }
    base.update(overrides)
    return PlanRequest(**base)


class ScriptedProvider:
    """Hands back proposals in the order a test wrote them.

    The last one repeats, so a test about a single plan does not have to think
    about how many times a replan might ask.
    """

    name = "scripted"

    def __init__(self, *proposals: PlanProposal) -> None:
        if not proposals:
            raise ValueError("a scripted provider needs at least one proposal")
        self._proposals = list(proposals)
        self.calls: list[PlanRequest] = []

    def propose(self, request: PlanRequest) -> PlanProposal:
        self.calls.append(request)
        index = min(len(self.calls) - 1, len(self._proposals) - 1)
        return self._proposals[index]


def session_id() -> str:
    return DEFAULT_SESSION
