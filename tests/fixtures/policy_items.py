"""Builders for the selection-policy tests (T017/T018/T019).

A policy test is about one field: this item is rotten, that book is finished.
Everything else is scaffolding, so these builders produce a healthy default —
an edible, fresh, unremarkable item in the main inventory — and each test
overrides only the field it is actually asserting on.

Defaults matter here in a way they do not in most fixtures: a builder whose
default item is already unusable would make a rejection test pass for the wrong
reason. Every default below therefore describes an item the policy accepts.
"""

from __future__ import annotations

from typing import Any

from pz_agent_core.protocol import (
    ContainerKind,
    ContainerView,
    InventoryView,
    ItemView,
    PlayerState,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    main_container_ref,
    make_container,
    make_player,
)

MAIN_REF = main_container_ref()
BACKPACK_REF = backpack_container_ref()
WORLD_REF = f"container:{DEFAULT_SESSION}:world:1200:3400:0:1:0"


def container_tail(container_ref: str) -> str:
    """The part of a container reference that an item reference embeds."""
    return container_ref.split(":", 2)[2]


def policy_item_ref(runtime_id: str, container_ref: str = MAIN_REF) -> str:
    return f"item:{DEFAULT_SESSION}:{container_tail(container_ref)}:{runtime_id}:0"


def main_container() -> ContainerView:
    return make_container(MAIN_REF, ContainerKind.PLAYER_MAIN, name="Inventory")


def backpack_container(**overrides: Any) -> ContainerView:
    return make_container(BACKPACK_REF, ContainerKind.WORN, name="Backpack", **overrides)


def world_container(**overrides: Any) -> ContainerView:
    return make_container(WORLD_REF, ContainerKind.WORLD, name="Counter", **overrides)


def inventory(*items: ItemView, containers: list[ContainerView] | None = None) -> InventoryView:
    """An inventory holding *items*, with the standard containers present."""
    return InventoryView(
        containers=containers
        if containers is not None
        else [main_container(), backpack_container()],
        items=list(items),
    )


def food_payload(**overrides: Any) -> dict[str, Any]:
    """A safe, fresh, filling tin of something."""
    payload: dict[str, Any] = {
        "edible": True,
        "destroyed": False,
        "hunger_change": -0.4,
        "thirst_change": 0.0,
        "calories": 400.0,
        "freshness": "fresh",
        "rot_progress": 0.0,
        "raw": False,
        "raw_unsafe": False,
        "requires_cooking": False,
        "cooked": True,
        "burnt": False,
        "burn_progress": 0.0,
        "poisonous": False,
        "tainted": False,
        "frozen": False,
        "unhappy_change": 0.0,
        "boredom_change": 0.0,
        "remaining_portions": 4,
        "total_portions": 4,
    }
    payload.update(overrides)
    return payload


def food_item(
    runtime_id: str,
    *,
    container_ref: str = MAIN_REF,
    display_name: str = "Tinned Beans",
    full_type: str = "Base.TinnedBeans",
    weight: float = 0.8,
    tags: list[str] | None = None,
    favorite: bool = False,
    extra: dict[str, Any] | None = None,
    **food_overrides: Any,
) -> ItemView:
    return ItemView(
        ref=policy_item_ref(runtime_id, container_ref),
        container_ref=container_ref,
        full_type=full_type,
        display_name=display_name,
        category="Food",
        weight=weight,
        favorite=favorite,
        tags=list(tags or []),
        food=food_payload(**food_overrides),
        extra=dict(extra or {}),
    )


def drink_payload(**overrides: Any) -> dict[str, Any]:
    """A full bottle of clean water."""
    payload: dict[str, Any] = {
        "drinkable": True,
        "destroyed": False,
        "type": "water",
        "water": True,
        "tainted": False,
        "poisonous": False,
        "alcoholic": False,
        "alcohol_units": 0.0,
        "thirst_change": -0.4,
        "remaining_units": 1.0,
        "capacity_units": 1.0,
        "unhappy_change": 0.0,
    }
    payload.update(overrides)
    return payload


def drink_item(
    runtime_id: str,
    *,
    container_ref: str = MAIN_REF,
    display_name: str = "Water Bottle",
    full_type: str = "Base.WaterBottleFull",
    weight: float = 0.8,
    tags: list[str] | None = None,
    favorite: bool = False,
    extra: dict[str, Any] | None = None,
    **fluid_overrides: Any,
) -> ItemView:
    return ItemView(
        ref=policy_item_ref(runtime_id, container_ref),
        container_ref=container_ref,
        full_type=full_type,
        display_name=display_name,
        category="Drink",
        weight=weight,
        favorite=favorite,
        tags=list(tags or []),
        fluid=drink_payload(**fluid_overrides),
        extra=dict(extra or {}),
    )


def literature_payload(**overrides: Any) -> dict[str, Any]:
    """An unread paperback: readable by anyone, teaching nothing."""
    payload: dict[str, Any] = {
        "kind": "generic",
        "skill": "",
        "min_level": 0,
        "max_level": 10,
        "pages_total": 200,
        "pages_read": 0,
        "already_read": False,
        "unread_recipes": 0,
        "boredom_change": -8.0,
        "unhappy_change": 0.0,
        "destroyed": False,
    }
    payload.update(overrides)
    return payload


def literature_item(
    runtime_id: str,
    *,
    container_ref: str = MAIN_REF,
    display_name: str = "Paperback",
    full_type: str = "Base.Book",
    weight: float = 0.5,
    tags: list[str] | None = None,
    favorite: bool = False,
    extra: dict[str, Any] | None = None,
    **literature_overrides: Any,
) -> ItemView:
    return ItemView(
        ref=policy_item_ref(runtime_id, container_ref),
        container_ref=container_ref,
        full_type=full_type,
        display_name=display_name,
        category="Literature",
        weight=weight,
        favorite=favorite,
        tags=list(tags or []),
        literature=literature_payload(**literature_overrides),
        extra=dict(extra or {}),
    )


def hungry_player(hunger: float = 0.5, **overrides: Any) -> PlayerState:
    stats: dict[str, Any] = {"hunger": hunger}
    stats.update(overrides.pop("stats", {}))
    return make_player(stats=stats, **overrides)


def thirsty_player(thirst: float = 0.5, **overrides: Any) -> PlayerState:
    stats: dict[str, Any] = {"thirst": thirst}
    stats.update(overrides.pop("stats", {}))
    return make_player(stats=stats, **overrides)


def reader(**stats: Any) -> PlayerState:
    """A player whose stats carry traits and skill levels."""
    return make_player(stats=dict(stats))
