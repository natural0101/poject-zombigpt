"""Builders for the game-adapter tests: worlds, squares and prepared commands.

An adapter test is about one fact — the square is not loaded, the bag is full,
the bottle is empty — and every one of them needs a whole observation to say it
in. These builders produce a world in which the action under test *works*, so a
failure test that passes does so because of the field it changed and not because
the baseline was already broken.

The one piece of real machinery here is :func:`prepare`. The engine ships the
command that ``build_args`` produced, so ``verify`` always sees prepared
arguments; a test that called ``verify`` with the raw draft would be exercising
a command shape that never reaches the mod.
"""

from __future__ import annotations

import uuid
from typing import Any

from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_LOADED,
    SQUARE_OBJECT_KIND,
)
from pz_agent_core.protocol import (
    ActionName,
    Command,
    ContainerKind,
    ContainerView,
    Hands,
    InventoryView,
    ItemView,
    JsonDict,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    main_container_ref,
    make_container,
    make_observation,
    make_player,
)

#: The square the default player stands on, matching ``make_player``.
HOME_X = 1200
HOME_Y = 3400
HOME_Z = 0

MAIN_REF = main_container_ref()
BAG_REF = f"container:{DEFAULT_SESSION}:carried:77001"
CRATE_REF = f"container:{DEFAULT_SESSION}:world:1200:3400:0:1:0"
FAR_CRATE_REF = f"container:{DEFAULT_SESSION}:world:1240:3400:0:1:0"


def container_tail(container_ref: str) -> str:
    """The part of a container reference that an item reference embeds."""
    return container_ref.split(":", 2)[2]


def item_ref_in(runtime_id: str, container_ref: str = MAIN_REF, generation: int = 0) -> str:
    return f"item:{DEFAULT_SESSION}:{container_tail(container_ref)}:{runtime_id}:{generation}"


def square_ref(x: int, y: int, z: int = HOME_Z) -> str:
    return f"square:{DEFAULT_SESSION}:{x}:{y}:{z}"


def object_ref(runtime_id: str) -> str:
    return f"object:{DEFAULT_SESSION}:{runtime_id}:0"


def main_container(**overrides: Any) -> ContainerView:
    return make_container(MAIN_REF, ContainerKind.PLAYER_MAIN, name="Inventory", **overrides)


def bag_container(**overrides: Any) -> ContainerView:
    return make_container(BAG_REF, ContainerKind.CARRIED, name="Duffel Bag", **overrides)


def crate_container(ref: str = CRATE_REF, **overrides: Any) -> ContainerView:
    return make_container(ref, ContainerKind.WORLD, name="Crate", **overrides)


def an_item(
    runtime_id: str = "42",
    *,
    container_ref: str = MAIN_REF,
    display_name: str = "Tinned Beans",
    weight: float = 0.8,
    generation: int = 0,
    **overrides: Any,
) -> ItemView:
    """One plain item, with its reference built around its container."""
    base: dict[str, Any] = {
        "ref": item_ref_in(runtime_id, container_ref, generation),
        "container_ref": container_ref,
        "full_type": "Base.TinnedBeans",
        "display_name": display_name,
        "category": "Food",
        "weight": weight,
    }
    base.update(overrides)
    return ItemView(**base)


def moved(item: ItemView, container_ref: str) -> ItemView:
    """The same object, as the mod would report it after a transfer.

    The reference is rebuilt around the new container, which is exactly why the
    adapters match on runtime id rather than on the reference string.
    """
    runtime_id, generation = item.ref.rsplit(":", 2)[-2:]
    return ItemView(
        ref=item_ref_in(runtime_id, container_ref, int(generation)),
        container_ref=container_ref,
        full_type=item.full_type,
        display_name=item.display_name,
        category=item.category,
        weight=item.weight,
        favorite=item.favorite,
        equipped=item.equipped,
        tags=list(item.tags),
        food=None if item.food is None else dict(item.food),
        literature=None if item.literature is None else dict(item.literature),
        fluid=None if item.fluid is None else dict(item.fluid),
        extra=dict(item.extra),
    )


def a_square(
    x: int,
    y: int,
    z: int = HOME_Z,
    *,
    semantics: list[str] | None = None,
) -> NearbyObject:
    """A grid square as ``world.inspect`` describes it: loaded unless said otherwise."""
    return NearbyObject(
        ref=square_ref(x, y, z),
        kind=SQUARE_OBJECT_KIND,
        distance=float(max(abs(x - HOME_X), abs(y - HOME_Y))),
        position=Position(x=float(x), y=float(y), z=z),
        semantics=[SEMANTIC_LOADED] if semantics is None else list(semantics),
    )


def a_world_object(
    ref: str,
    *,
    x: int = HOME_X + 3,
    y: int = HOME_Y,
    z: int = HOME_Z,
    kind: str = "container",
    distance: float | None = None,
    semantics: list[str] | None = None,
    open: bool | None = None,
    locked: bool | None = None,
    barricaded: bool | None = None,
    orientation: str | None = None,
) -> NearbyObject:
    return NearbyObject(
        ref=ref,
        kind=kind,
        distance=float(max(abs(x - HOME_X), abs(y - HOME_Y))) if distance is None else distance,
        position=Position(x=float(x), y=float(y), z=z),
        semantics=list(semantics or ["container"]),
        open=open,
        locked=locked,
        barricaded=barricaded,
        orientation=orientation,
    )


def a_world(
    *,
    seq: int = 1,
    items: list[ItemView] | None = None,
    containers: list[ContainerView] | None = None,
    objects: list[NearbyObject] | None = None,
    position: Position | None = None,
    stats: dict[str, Any] | None = None,
    hands: Hands | None = None,
    inventory: InventoryView | None = None,
    nearby: NearbyView | None = None,
    no_inventory: bool = False,
    no_nearby: bool = False,
    **overrides: Any,
) -> Observation:
    """An observation in which the adapters' happy paths hold.

    ``no_inventory`` and ``no_nearby`` are separate flags rather than passing
    ``None``, because "the mod did not produce this tier" and "the tier is
    empty" are different facts and several adapters answer them differently.
    """
    player = make_player(
        position=position or Position(x=float(HOME_X), y=float(HOME_Y), z=HOME_Z, direction="S"),
        stats=dict(stats or {}),
        hands=hands or Hands(),
    )
    built_inventory = (
        inventory
        if inventory is not None
        else InventoryView(
            containers=containers if containers is not None else [main_container()],
            items=list(items or []),
        )
    )
    built_nearby = nearby if nearby is not None else NearbyView(objects=list(objects or []))
    return make_observation(
        seq=seq,
        player=player,
        inventory=None if no_inventory else built_inventory,
        nearby=None if no_nearby else built_nearby,
        **overrides,
    )


def a_command(action: ActionName, args: JsonDict, *, session_id: str = DEFAULT_SESSION) -> Command:
    return Command(
        session_id=session_id,
        seq=0,
        command_id=str(uuid.uuid4()),
        idempotency_key="adapter-test",
        issued_at_ms=1_700_000_000_000,
        lease_ms=15_000,
        action=action,
        args=args,
    )


def prepare(adapter: Any, command: Command, observation: Observation) -> Command:
    """The command as the mod receives it — what ``verify`` is always given."""
    args: JsonDict = adapter.build_args(command, observation)
    return Command(
        session_id=command.session_id,
        seq=command.seq,
        command_id=command.command_id,
        idempotency_key=command.idempotency_key,
        issued_at_ms=command.issued_at_ms,
        lease_ms=command.lease_ms,
        action=command.action,
        args=args,
        expected_observation_seq=observation.seq,
        policy=command.policy,
    )
