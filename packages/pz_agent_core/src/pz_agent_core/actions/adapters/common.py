"""Preconditions, reference resolution and reach rules shared by the adapters.

Every adapter in this package answers the same three questions — is the command
well formed, what does the mod receive, and what observable change proves it
happened — and the first of the three is very nearly the same each time. It
lives here so that "this reference was minted by another session" is one refusal
with one reason code across movement, transfer, eating, drinking and reading,
instead of five that differ by accident.

Invariant: nothing in this module *chooses* anything. It resolves what a command
already names, and refuses what it cannot resolve. Which sandwich, which book
and which square are decisions of :mod:`pz_agent_core.policy`, and they stay
there — an adapter that started picking items would be a second, untested copy
of the selection rules.

Two conventions carry most of the weight:

* **A missing observation tier is never optimism.** If the mod did not report an
  inventory, the answer is ``CAPABILITY_UNAVAILABLE``, not "assume it is fine" —
  there would be no way to verify the postcondition either.
* **Identity is the runtime id, not the reference string.** An item reference
  embeds the container it sits in (``item:<session>:<container-tail>:<id>:<gen>``),
  so moving an item *changes its reference*. Anything that has to recognise the
  same object before and after an action compares :class:`ItemIdentity`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from ...protocol import (
    ON_PERSON_CONTAINERS,
    ActionName,
    Command,
    ContainerKind,
    ContainerRef,
    ContainerView,
    InventoryView,
    ItemRef,
    ItemView,
    JsonDict,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
    RefError,
    RefKind,
    belongs_to_session,
    ref_kind,
)
from ..adapter import PreconditionFailed

__all__ = [
    "MAX_CONTAINER_DEPTH",
    "MAX_PREREQUISITES",
    "ContainerChain",
    "ItemIdentity",
    "Prerequisite",
    "check_args",
    "container_chain",
    "container_kind_is_world",
    "describe_container",
    "find_by_identity",
    "grid_distance",
    "identity_of",
    "nearby_object",
    "plane_distance",
    "player_main",
    "read_count",
    "read_flag",
    "read_number",
    "read_position",
    "read_ref",
    "refused",
    "require_inventory",
    "require_nearby",
    "resolve_container",
    "resolve_item",
    "square_of",
    "world_square",
]

#: How deep a container tree may nest before the walk gives up. A bag in a bag
#: in a bag is real; eight levels is not, and an unbounded walk over data the
#: mod produced is exactly the kind of loop AGENTS.md forbids.
MAX_CONTAINER_DEPTH: Final = 8

#: Longest prerequisite list a refusal reports. A refusal is rendered to a user
#: and written to a log, so it is bounded like every other diagnostic.
MAX_PREREQUISITES: Final = 4


@dataclass(frozen=True, slots=True)
class Prerequisite:
    """A command that must succeed before the refused one can be re-issued.

    Adapters refuse rather than silently doing the preparation themselves. An
    adapter that quietly transferred an item before eating it would be running
    a two-step plan behind one command id, which makes the failure of the first
    step indistinguishable from the failure of the second and leaves the caller
    unable to say what actually happened.
    """

    action: ActionName
    args: JsonDict
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a prerequisite must explain why it is needed")

    def as_dict(self) -> JsonDict:
        return {"action": self.action.value, "args": dict(self.args), "detail": self.detail}


def refused(
    message: str,
    *,
    reason_code: ReasonCode,
    prerequisites: Iterable[Prerequisite] = (),
    evidence: JsonDict | None = None,
) -> PreconditionFailed:
    """Build a refusal that carries its prerequisites as structured evidence."""
    payload: JsonDict = dict(evidence or {})
    listed = tuple(prerequisites)[:MAX_PREREQUISITES]
    if listed:
        payload["prerequisites"] = [p.as_dict() for p in listed]
    return PreconditionFailed(message, reason_code=reason_code, evidence=payload)


# --------------------------------------------------------------------------
# argument readers
# --------------------------------------------------------------------------
# The args object is free-form on the wire (``schemas/command.schema.json``
# types it as a bare object), so every reader below is strict: an argument the
# adapter does not understand is a refusal, never something quietly dropped.
# Dropping an unknown ``allow_windows`` would turn a command the caller
# believed was permissive into a different action than they asked for.


def check_args(command: Command, *, allowed: Iterable[str], required: Iterable[str] = ()) -> None:
    """Refuse unknown and missing arguments before anything is resolved."""
    unknown = sorted(set(command.args) - set(allowed))
    if unknown:
        raise PreconditionFailed(
            f"unsupported argument(s): {unknown}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    missing = sorted(key for key in required if command.args.get(key) is None)
    if missing:
        raise PreconditionFailed(
            f"missing required argument(s): {missing}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )


def read_ref(command: Command, key: str, *, kind: RefKind) -> str:
    """Read a reference argument of *kind* that this session minted.

    A reference from an earlier session is not stale but *wrong*: its runtime
    ids may now denote different objects, so it is ``INVALID_REF`` rather than
    anything retryable.
    """
    raw = command.args.get(key)
    if not isinstance(raw, str) or not raw:
        raise PreconditionFailed(
            f"{key} must be a non-empty reference string",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    try:
        actual = ref_kind(raw)
    except RefError as exc:
        raise PreconditionFailed(
            f"{key} is not a reference: {raw!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    if actual is not kind:
        raise PreconditionFailed(
            f"{key} must be a {kind.value} reference, got a {actual.value} reference",
            reason_code=ReasonCode.INVALID_REF,
        )
    if not belongs_to_session(raw, command.session_id):
        raise PreconditionFailed(
            f"{key} was minted by a different session",
            reason_code=ReasonCode.INVALID_REF,
        )
    return raw


def read_number(
    command: Command,
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read a bounded numeric argument, refusing anything outside the range."""
    value = command.args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreconditionFailed(f"{key} must be a number", reason_code=ReasonCode.INVALID_ARGUMENT)
    if not minimum <= value <= maximum:
        raise PreconditionFailed(
            f"{key} must be within {minimum}..{maximum}, got {value}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return float(value)


def read_count(command: Command, key: str, *, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer argument. ``bool`` is not an integer here."""
    value = command.args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreconditionFailed(
            f"{key} must be an integer", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    if not minimum <= value <= maximum:
        raise PreconditionFailed(
            f"{key} must be within {minimum}..{maximum}, got {value}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return value


def read_flag(command: Command, key: str, *, default: bool) -> bool:
    value = command.args.get(key, default)
    if not isinstance(value, bool):
        raise PreconditionFailed(
            f"{key} must be a boolean", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    return value


def read_position(value: Any, *, field_name: str) -> tuple[int, int, int]:
    """Read an ``{x, y, z}`` grid square out of an argument object.

    Squares are integral. Accepting a float would let a caller aim between two
    cells and then be told, correctly but uselessly, that no such square is
    loaded.
    """
    if not isinstance(value, dict):
        raise PreconditionFailed(
            f"{field_name} must be an object with x, y and z",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    coordinates: list[int] = []
    for axis in ("x", "y", "z"):
        raw = value.get(axis)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise PreconditionFailed(
                f"{field_name}.{axis} must be an integer square coordinate",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        coordinates.append(raw)
    return coordinates[0], coordinates[1], coordinates[2]


# --------------------------------------------------------------------------
# observation resolution
# --------------------------------------------------------------------------


def require_inventory(observation: Observation) -> InventoryView:
    """The inventory view, or a refusal saying it cannot be verified.

    ``CAPABILITY_UNAVAILABLE`` rather than a precondition failure: an
    observation without an inventory tier means the *evidence* for any item
    action is missing, so the action could not be proven even if it worked.
    """
    inventory = observation.inventory
    if inventory is None:
        raise PreconditionFailed(
            "the mod did not report an inventory, so no item action can be verified",
            reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
            evidence={"observation_seq": observation.seq},
        )
    return inventory


def require_nearby(observation: Observation) -> NearbyView:
    """The nearby view, or a refusal saying movement cannot be verified."""
    nearby = observation.nearby
    if nearby is None:
        raise PreconditionFailed(
            "the mod did not report the surroundings, so movement cannot be verified",
            reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
            evidence={"observation_seq": observation.seq},
        )
    return nearby


def resolve_item(
    inventory: InventoryView, item_ref: str, *, field_name: str = "item_ref"
) -> ItemView:
    """Find the item the command names, or refuse with ``INVALID_REF``."""
    item = inventory.item(item_ref)
    if item is None:
        raise PreconditionFailed(
            f"{field_name} {item_ref} is not in the observed inventory",
            reason_code=ReasonCode.INVALID_REF,
            evidence={"item_ref": item_ref},
        )
    return item


def resolve_container(
    inventory: InventoryView, container_ref: str, *, field_name: str
) -> ContainerView:
    """Find the container the command names, or refuse with ``INVALID_REF``."""
    container = inventory.container(container_ref)
    if container is None:
        raise PreconditionFailed(
            f"{field_name} {container_ref} is not in the observed container tree",
            reason_code=ReasonCode.INVALID_REF,
            evidence={"container_ref": container_ref},
        )
    return container


def player_main(inventory: InventoryView) -> ContainerView:
    """The main inventory container, or a refusal.

    Its absence is not a precondition the caller can fix — every item action
    ends up needing it — so it is reported as the missing capability it is.
    """
    main = inventory.main_container()
    if main is None:
        raise PreconditionFailed(
            "the mod did not report a player-main container",
            reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
        )
    return main


@dataclass(frozen=True, slots=True)
class ContainerChain:
    """A container and its ancestors, outermost last.

    ``complete`` is false when a declared ``parent_ref`` was not reported. That
    distinction matters: an incomplete chain means the mod knows the container
    hangs off something it did not describe, which is not the same as a
    container that hangs off nothing.
    """

    containers: tuple[ContainerView, ...]
    complete: bool

    @property
    def root(self) -> ContainerView:
        return self.containers[-1]

    @property
    def on_person(self) -> bool:
        """True only when every link travels with the character.

        Checked over the whole chain rather than the leaf: a shoulder bag
        sitting inside a world crate is still a ``carried`` container, and
        treating it as on-person would promise reach the character does not
        have.
        """
        return self.complete and all(c.kind in ON_PERSON_CONTAINERS for c in self.containers)

    @property
    def accessible(self) -> bool:
        return self.complete and all(c.accessible for c in self.containers)

    def as_dict(self) -> JsonDict:
        return {
            "chain": [c.ref for c in self.containers],
            "complete": self.complete,
            "on_person": self.on_person,
        }


def container_chain(inventory: InventoryView, container: ContainerView) -> ContainerChain:
    """Walk *container* outward to its root, bounded and cycle-safe.

    Raises:
        PreconditionFailed: when the tree is deeper than
            :data:`MAX_CONTAINER_DEPTH` or contains a cycle. Both mean the
            container view cannot be reasoned about, and guessing past either
            one is how an unbounded walk gets written.
    """
    chain: list[ContainerView] = [container]
    seen: set[str] = {container.ref}
    current = container
    while current.parent_ref is not None:
        if len(chain) >= MAX_CONTAINER_DEPTH:
            raise PreconditionFailed(
                f"container {container.ref} nests deeper than {MAX_CONTAINER_DEPTH} levels",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"chain": [c.ref for c in chain]},
            )
        if current.parent_ref in seen:
            raise PreconditionFailed(
                f"the reported container tree loops back to {current.parent_ref}",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"chain": [c.ref for c in chain]},
            )
        parent = inventory.container(current.parent_ref)
        if parent is None:
            return ContainerChain(containers=tuple(chain), complete=False)
        seen.add(parent.ref)
        chain.append(parent)
        current = parent
    return ContainerChain(containers=tuple(chain), complete=True)


# --------------------------------------------------------------------------
# item identity across a move
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemIdentity:
    """The part of an item reference that survives the item being moved.

    The container tail does not: an item's reference is rebuilt around whatever
    container holds it, so after a transfer the *same object* answers to a
    different string. ``generation`` stays in the identity because a bump means
    a save/load boundary, after which equal runtime ids say nothing.
    """

    runtime_id: str
    generation: int


def identity_of(item_ref: str, *, field_name: str = "item_ref") -> ItemIdentity:
    """Parse an item reference into its stable identity."""
    try:
        parsed = ItemRef.parse(item_ref)
    except RefError as exc:
        raise PreconditionFailed(
            f"{field_name} is not a well-formed item reference: {item_ref!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    return ItemIdentity(runtime_id=parsed.runtime_id, generation=parsed.generation)


def find_by_identity(inventory: InventoryView, identity: ItemIdentity) -> tuple[ItemView, ...]:
    """Every observed item that *is* this object, whatever container it sits in.

    Returns a tuple rather than an item because "how many" is the interesting
    number: more than one entry with the same runtime id is a duplication, and
    a duplication must never be reported as a successful move.
    """
    found: list[ItemView] = []
    for item in inventory.items:
        try:
            parsed = ItemRef.parse(item.ref)
        except RefError:
            # A reference the mod sent that this side cannot parse is dropped
            # rather than raised on: it can only ever fail to match, and one
            # malformed entry must not blind the search for the others.
            continue
        if parsed.runtime_id == identity.runtime_id and parsed.generation == identity.generation:
            found.append(item)
    return tuple(found)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


def square_of(position: Position) -> tuple[int, int, int]:
    """The grid square a world position stands on."""
    return math.floor(position.x), math.floor(position.y), position.z


def grid_distance(position: Position, x: int, y: int) -> int:
    """Chebyshev distance in squares — the metric a travel budget is in."""
    px, py, _ = square_of(position)
    return max(abs(px - x), abs(py - y))


def plane_distance(position: Position, x: float, y: float) -> float:
    """Euclidean distance on the floor plane — the metric an arrival radius is in.

    Two metrics on purpose: ``max_distance`` bounds how far the character is
    asked to walk and is naturally counted in squares, while ``radius`` is a
    radius and a Chebyshev "radius" is a box.
    """
    return math.hypot(position.x - x, position.y - y)


def world_square(container_ref: str) -> tuple[int, int, int] | None:
    """The square a world container stands on, or None when it is not one.

    The coordinates come out of the reference tail
    (``world:<x>:<y>:<z>:<object>:<container>``) rather than from a lookup,
    because a world container is exactly the thing that may not be in the
    inventory view until the character is standing next to it.
    """
    try:
        parsed = ContainerRef.parse(container_ref)
    except RefError:
        return None
    if not parsed.is_world:
        return None
    parts = parsed.tail.split(":")
    if len(parts) != 6:
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None


def nearby_object(nearby: NearbyView, ref: str) -> NearbyObject | None:
    """The world object with this reference, or None when it is out of view."""
    return next((o for o in nearby.objects if o.ref == ref), None)


def container_kind_is_world(container: ContainerView) -> bool:
    """True for containers that need travel and a world-touching permission."""
    return container.kind not in ON_PERSON_CONTAINERS


def describe_container(container: ContainerView) -> JsonDict:
    """The origin metadata a caller needs to put an item back where it was."""
    payload: JsonDict = {
        "container_ref": container.ref,
        "container_kind": container.kind.value,
        "container_name": container.name,
    }
    if container.kind is ContainerKind.WORLD:
        square = world_square(container.ref)
        if square is not None:
            payload["square"] = {"x": square[0], "y": square[1], "z": square[2]}
    return payload
