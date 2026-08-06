"""``container.inspect`` and ``container.open_nearby``: looking inside things.

The two are deliberately different actions rather than one with a flag, because
one of them touches the world and the other does not (§5.10). Reading what a
container holds is a query the mod answers off the engine's own item list — no
UI is driven, nothing is clicked, and the action is therefore read-only in the
protocol's sense. Getting *to* a container is a walk, and a walk is the
character moving through a world with zombies in it.

Their postconditions follow from that split:

* ``container.inspect`` is proven by the observation naming the container that
  was asked about. Its evidence is what the read saw, and a container the mod
  did not report is no evidence at all — "the crate is empty" and "the crate was
  not described" are opposite facts, and only the second one is a failure.
* ``container.open_nearby`` is proven by the container being observed *open to
  the character*: reported, accessible, and with the character standing inside
  the reach that was asked for. An ack saying the walk finished is a statement
  about the queue.

Neither adapter lists a container's contents by walking the world itself. Both
read the inventory tier the observer already produces, which is the same list
the mod would enumerate and the only one this side can verify against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import MOVE_TO_SQUARE
from ...protocol import (
    ActionName,
    Command,
    ContainerRef,
    ContainerView,
    InventoryView,
    JsonDict,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    check_args,
    container_chain,
    grid_distance,
    plane_distance,
    read_count,
    read_number,
    read_ref,
    require_inventory,
    resolve_container,
    world_square,
)
from .movement import MAX_ARRIVAL_RADIUS, MAX_MOVE_DISTANCE_SQUARES

__all__ = [
    "DEFAULT_INSPECT_POLL_MS",
    "DEFAULT_INSPECT_TIMEOUT_MS",
    "DEFAULT_OPEN_POLL_MS",
    "DEFAULT_OPEN_RADIUS",
    "DEFAULT_OPEN_TIMEOUT_MS",
    "MAX_LISTED_ITEMS",
    "MIN_OPEN_RADIUS",
    "ContainerInspectAdapter",
    "ContainerOpenNearbyAdapter",
]

#: Items one inspection reports. The listing travels in an ack, so a warehouse
#: shelf would otherwise put a thousand descriptors in one message. The mod
#: applies the same ceiling in ``adapters/Containers.lua``.
MAX_LISTED_ITEMS: Final = 64

#: Reach for opening: close enough to take something out of it. The floor keeps
#: a caller from naming a radius no position ever satisfies; the ceiling is
#: movement's own, so "nearby" cannot come to mean the far side of the room.
DEFAULT_OPEN_RADIUS: Final = 1.6
MIN_OPEN_RADIUS: Final = 0.1

DEFAULT_INSPECT_TIMEOUT_MS: Final = 5_000
DEFAULT_INSPECT_POLL_MS: Final = 100

DEFAULT_OPEN_TIMEOUT_MS: Final = 30_000
DEFAULT_OPEN_POLL_MS: Final = 250


def _describe_items(inventory: InventoryView, container: ContainerView, limit: int) -> JsonDict:
    """The bounded listing that makes up an inspection's findings.

    ``total`` is the untruncated count. A listing that quietly showed the first
    sixty-four of two hundred would read as a complete answer while being a
    partial one, and a planner deciding whether to keep searching needs the real
    number.
    """
    held = inventory.items_in(container.ref)
    listed = [
        {
            "item_ref": item.ref,
            "full_type": item.full_type,
            "display_name": item.display_name,
            "weight": item.weight,
            "equipped": item.equipped,
        }
        for item in held[:limit]
    ]
    return {
        "items": listed,
        "item_count": len(listed),
        "total_items": len(held),
        "truncated": len(held) > len(listed),
        "capacity": container.capacity,
        "used_capacity": container.used_capacity,
    }


@dataclass(frozen=True, slots=True)
class _InspectSpec:
    container_ref: str
    limit: int

    @classmethod
    def parse(cls, command: Command) -> _InspectSpec:
        check_args(command, allowed=("container_ref", "limit"), required=("container_ref",))
        return cls(
            container_ref=read_ref(command, "container_ref", kind=RefKind.CONTAINER),
            limit=read_count(
                command, "limit", default=MAX_LISTED_ITEMS, minimum=1, maximum=MAX_LISTED_ITEMS
            ),
        )

    def as_args(self) -> JsonDict:
        return {"container_ref": self.container_ref, "limit": self.limit}


@dataclass(frozen=True, slots=True)
class ContainerInspectAdapter:
    """``container.inspect``: read what one container holds.

    Read-only, so it names no ``required_capability``: what it needs is the
    observation's inventory tier, and :func:`require_inventory` refuses with
    ``CAPABILITY_UNAVAILABLE`` when the mod did not produce one — which is the
    honest gate, and the same one every item action already uses.
    """

    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_INSPECT_POLL_MS

    name: ClassVar[ActionName] = ActionName.CONTAINER_INSPECT
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _InspectSpec.parse(command)
        inventory = require_inventory(observation)
        # Resolved rather than trusted: a container the observer has not
        # described is one whose contents this side could not check the report
        # against, whatever the mod sends back.
        resolve_container(inventory, spec.container_ref, field_name="container_ref")

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _InspectSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _InspectSpec.parse(command)
        if after.inventory is None:
            return None
        container = after.inventory.container(spec.container_ref)
        if container is None:
            return None
        observed: JsonDict = {
            "container_ref": container.ref,
            "container_kind": container.kind.value,
            "container_name": container.name,
            "accessible": container.accessible,
        }
        observed.update(_describe_items(after.inventory, container, spec.limit))
        return Evidence(
            kind="container_contents_described",
            observation_seq=after.seq,
            observed=observed,
        )


@dataclass(frozen=True, slots=True)
class _OpenSpec:
    container_ref: str
    radius: float

    @classmethod
    def parse(cls, command: Command) -> _OpenSpec:
        check_args(command, allowed=("container_ref", "radius"), required=("container_ref",))
        return cls(
            container_ref=read_ref(command, "container_ref", kind=RefKind.CONTAINER),
            radius=read_number(
                command,
                "radius",
                default=DEFAULT_OPEN_RADIUS,
                minimum=MIN_OPEN_RADIUS,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
        )

    def as_args(self) -> JsonDict:
        return {"container_ref": self.container_ref, "radius": self.radius}


def _open_target_square(spec: _OpenSpec) -> tuple[int, int, int]:
    """The square the container stands on, or a refusal naming why there is none.

    Two refusals rather than one: a container the character already carries has
    nothing to walk to, and a container that is neither carried nor locatable in
    the world cannot have its reach checked at all. "Probably close enough" is
    how an action gets queued that fails at the far end.
    """
    try:
        parsed = ContainerRef.parse(spec.container_ref)
    except RefError as exc:
        raise PreconditionFailed(
            f"container_ref is not a well-formed container reference: {spec.container_ref!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    if parsed.is_on_person:
        raise PreconditionFailed(
            "a container the character is carrying is already within reach",
            reason_code=ReasonCode.INVALID_ARGUMENT,
            evidence={"container_ref": spec.container_ref},
        )
    square = world_square(spec.container_ref)
    if square is None:
        raise PreconditionFailed(
            f"{spec.container_ref} names no square the character could walk to",
            reason_code=ReasonCode.INVALID_ARGUMENT,
            evidence={"container_ref": spec.container_ref},
        )
    return square


@dataclass(frozen=True, slots=True)
class ContainerOpenNearbyAdapter:
    """``container.open_nearby``: get within reach of a world container.

    Not read-only, and the name is the only thing about it that reads like a
    query: what it does is move the character. It therefore rides on the
    movement capability rather than claiming one of its own — walking to the
    container is the whole of its work in the world, and inventing a second
    probe for the same ``ISWalkToTimedAction`` would let one of the two say the
    walk is available while the other says it is not.

    A door in the way is not opened by this. The door is a different object and
    opening one is a different action, so the reach check simply fails and says
    so, rather than a door being opened that nobody asked about.
    """

    timeout_ms: int = DEFAULT_OPEN_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_OPEN_POLL_MS

    name: ClassVar[ActionName] = ActionName.CONTAINER_OPEN_NEARBY
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = MOVE_TO_SQUARE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _OpenSpec.parse(command)
        square = _open_target_square(spec)
        distance = grid_distance(observation.player.position, square[0], square[1])
        if distance > MAX_MOVE_DISTANCE_SQUARES:
            raise PreconditionFailed(
                f"the container is {distance} squares away, past the "
                f"{MAX_MOVE_DISTANCE_SQUARES} a single approach covers",
                reason_code=ReasonCode.TARGET_OUT_OF_RANGE,
                evidence={
                    "container_ref": spec.container_ref,
                    "distance_squares": distance,
                    "max_distance": MAX_MOVE_DISTANCE_SQUARES,
                },
            )
        inventory = observation.inventory
        if inventory is None:
            return
        container = inventory.container(spec.container_ref)
        if container is not None and not container_chain(inventory, container).accessible:
            # Reported and locked, boarded or otherwise shut: walking up to it
            # would satisfy the distance and none of the point.
            raise PreconditionFailed(
                f"{container.name} is not accessible right now",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"container_ref": container.ref},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _OpenSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _OpenSpec.parse(command)
        square = _open_target_square(spec)
        if after.inventory is None:
            return None
        container = after.inventory.container(spec.container_ref)
        if container is None or not container.accessible:
            return None
        position = after.player.position
        if position.z != square[2]:
            return None
        distance = plane_distance(position, float(square[0]), float(square[1]))
        if distance > spec.radius:
            return None
        observed: JsonDict = {
            "container_ref": container.ref,
            "container_kind": container.kind.value,
            "container_name": container.name,
            "accessible": True,
            "square": {"x": square[0], "y": square[1], "z": square[2]},
            "distance": distance,
            "radius": spec.radius,
            "position": position.to_dict(),
        }
        observed.update(_describe_items(after.inventory, container, MAX_LISTED_ITEMS))
        return Evidence(
            kind="container_within_reach",
            observation_seq=after.seq,
            observed=observed,
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        """Fraction of the original gap to the container that has closed.

        The generic stall detector watches the action queue, which keeps looking
        alive while a character is wedged against a fence. Distance is the
        measure that stops moving.
        """
        spec = _OpenSpec.parse(command)
        square = _open_target_square(spec)
        start = plane_distance(before.player.position, float(square[0]), float(square[1]))
        if start <= spec.radius:
            return 1.0
        remaining = plane_distance(latest.player.position, float(square[0]), float(square[1]))
        return min(1.0, max(0.0, (start - remaining) / start))
