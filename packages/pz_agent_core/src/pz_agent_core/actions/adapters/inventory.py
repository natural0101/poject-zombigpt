"""``inventory.transfer`` and ``inventory.ensure_main``: moving one item, once.

The postcondition is stated in two halves because only the pair is safe: the
item must be observed *inside the destination* **and** there must be exactly one
of it. An item reference embeds the container that holds it, so a transfer
re-mints the reference; matching by string would silently pass a duplicate,
which is the single worst outcome an inventory action can have. Matching by
runtime id and counting the matches turns "it duplicated" from an invisible
success into a refusal to claim anything.

Three refusals are structural rather than advisory:

* an equipped item is never moved implicitly — the caller is handed the
  ``inventory.unequip`` prerequisite instead (§5.12);
* a container is never put inside itself, however deep the nesting;
* a world container out of arm's reach yields a walk-to-it prerequisite rather
  than a transfer that would fail at the far end (§4.6).

``inventory.ensure_main`` is the same machinery with the destination fixed to
the main inventory. It is the preparation step every consumption and reading
action depends on (§4.7), which is why it is an action with its own command id
and its own evidence rather than something the eat adapter does on the side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities import INVENTORY_TRANSFER
from ...protocol import (
    ActionName,
    Command,
    ContainerView,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
    ReasonCode,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    ContainerChain,
    ItemIdentity,
    Prerequisite,
    check_args,
    container_chain,
    container_kind_is_world,
    describe_container,
    find_by_identity,
    grid_distance,
    identity_of,
    player_main,
    read_count,
    read_ref,
    refused,
    require_inventory,
    resolve_container,
    resolve_item,
    world_square,
)

__all__ = [
    "DEFAULT_ENSURE_MAIN_TIMEOUT_MS",
    "DEFAULT_TRANSFER_POLL_MS",
    "DEFAULT_TRANSFER_TIMEOUT_MS",
    "MAX_TRANSFER_QUANTITY",
    "WORLD_REACH_SQUARES",
    "EnsureMainAdapter",
    "TransferAdapter",
    "unequip_prerequisite",
]

#: One reference names one item, so one command moves one item. Bulk movement
#: is a plan of several commands, each individually verifiable — which is the
#: point: a partially completed batch under a single command id has no honest
#: terminal result.
MAX_TRANSFER_QUANTITY: Final = 1

#: Squares within which a world container counts as reachable without walking.
WORLD_REACH_SQUARES: Final = 1

#: Upper bound the ``quantity`` argument is read against, so that a caller who
#: asks for ten gets the explanation rather than a range error.
_QUANTITY_CEILING: Final = 64

DEFAULT_TRANSFER_TIMEOUT_MS: Final = 10_000
DEFAULT_TRANSFER_POLL_MS: Final = 200

DEFAULT_ENSURE_MAIN_TIMEOUT_MS: Final = 15_000


def unequip_prerequisite(item: ItemView) -> Prerequisite:
    """The preparation an equipped item needs before it can be moved."""
    return Prerequisite(
        action=ActionName.INVENTORY_UNEQUIP,
        args={"item_ref": item.ref},
        detail=f"{item.display_name} is equipped and must be put away first",
    )


def _approach_prerequisite(container: ContainerView, square: tuple[int, int, int]) -> Prerequisite:
    """Walk to the container's own square with an adjacency radius.

    ``movement.move_near`` names a world *object* reference, and a container
    reference is not one; the square the container stands on is derivable from
    the container reference itself, so the runnable form of "go to it" is a
    ``move_to`` whose radius is arm's length.
    """
    return Prerequisite(
        action=ActionName.MOVEMENT_MOVE_TO,
        args={
            "target": {"x": square[0], "y": square[1], "z": square[2]},
            "radius": float(WORLD_REACH_SQUARES),
        },
        detail=f"{container.name} is out of reach; walk up to it first",
    )


def _check_equipped(item: ItemView, observation: Observation) -> None:
    """Refuse to move something the character is wearing or holding."""
    if not item.equipped and not observation.player.hands.holds(item.ref):
        return
    raise refused(
        f"{item.display_name} is equipped; moving it needs an explicit unequip first",
        reason_code=ReasonCode.POLICY_DENIED,
        prerequisites=(unequip_prerequisite(item),),
        evidence={"item_ref": item.ref, "equipped": True},
    )


def _check_reachable(
    chain: ContainerChain,
    observation: Observation,
    *,
    field_name: str,
) -> None:
    """Refuse a container the character cannot actually open right now."""
    container = chain.containers[0]
    if not chain.complete:
        raise PreconditionFailed(
            f"{field_name} {container.ref} hangs off a container the mod did not report",
            reason_code=ReasonCode.INVALID_REF,
            evidence=chain.as_dict(),
        )
    if not chain.accessible:
        raise PreconditionFailed(
            f"{field_name} {container.name} is not accessible right now",
            reason_code=ReasonCode.PRECONDITION_FAILED,
            evidence=chain.as_dict(),
        )
    if chain.on_person:
        return
    square = world_square(chain.root.ref)
    if square is None:
        # A container that is neither on the person nor locatable in the world
        # cannot be checked for reach, and "probably close enough" is the kind
        # of assumption that fails halfway through a queued action.
        raise PreconditionFailed(
            f"{field_name} {container.name} is off-person and has no world position to reach",
            reason_code=ReasonCode.PRECONDITION_FAILED,
            evidence=chain.as_dict(),
        )
    distance = grid_distance(observation.player.position, square[0], square[1])
    if distance > WORLD_REACH_SQUARES or observation.player.position.z != square[2]:
        raise refused(
            f"{field_name} {container.name} is {distance} squares away on floor {square[2]}",
            reason_code=ReasonCode.PRECONDITION_FAILED,
            prerequisites=(_approach_prerequisite(chain.root, square),),
            evidence={**chain.as_dict(), "distance_squares": distance},
        )


def _check_capacity(destination: ContainerView, item: ItemView) -> None:
    """Refuse a move the destination has no room for.

    A container whose capacity the game does not report is let through: this
    check exists to avoid queueing a doomed action, and inventing a limit would
    refuse a move that would have worked.
    """
    free = destination.free_capacity
    if free is None:
        return
    if item.weight > free:
        raise PreconditionFailed(
            f"{destination.name} has {free:.2f} free and {item.display_name} weighs {item.weight}",
            reason_code=ReasonCode.CONTAINER_FULL,
            evidence={
                "container_ref": destination.ref,
                "free_capacity": free,
                "item_weight": item.weight,
            },
        )


def _check_not_into_itself(
    inventory: InventoryView, item: ItemView, destination: ContainerView
) -> None:
    """Refuse putting a bag inside a container that the bag itself provides.

    A carried container's reference tail is ``carried:<item runtime id>``, so
    the item that *is* the bag can be recognised anywhere in the destination's
    ancestry. Without this the move would either be refused by the game halfway
    through or, worse, succeed and orphan everything inside.
    """
    provided_tail = f"carried:{identity_of(item.ref).runtime_id}"
    for ancestor in container_chain(inventory, destination).containers:
        if ancestor.ref.endswith(provided_tail):
            raise PreconditionFailed(
                f"{item.display_name} cannot be put inside itself",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "container_ref": ancestor.ref},
            )


def _transfer_evidence(
    kind: str,
    *,
    identity: ItemIdentity,
    origin: JsonDict,
    destination: ContainerView,
    moved: ItemView,
    after: Observation,
) -> Evidence:
    return Evidence(
        kind=kind,
        observation_seq=after.seq,
        observed={
            "item_ref": moved.ref,
            "runtime_id": identity.runtime_id,
            "container_ref": destination.ref,
            "container_kind": destination.kind.value,
            "display_name": moved.display_name,
            "origin": origin,
        },
    )


def _verify_landed(
    identity: ItemIdentity,
    destination_ref: str,
    after: Observation,
) -> ItemView | None:
    """The moved item, once it is observed in *destination_ref* and nowhere else.

    None covers both "not there yet" and "there twice". The second is not a
    partial success to wait out — it is the duplication this adapter exists to
    never report — so it is reported as no evidence, and the attempt ends as a
    postcondition failure with the world state attached.
    """
    if after.inventory is None:
        return None
    matches = find_by_identity(after.inventory, identity)
    if len(matches) != 1:
        return None
    landed = matches[0]
    return landed if landed.container_ref == destination_ref else None


@dataclass(frozen=True, slots=True)
class _TransferSpec:
    item_ref: str
    source_ref: str | None
    destination_ref: str

    @classmethod
    def parse(cls, command: Command) -> _TransferSpec:
        check_args(
            command,
            allowed=(
                "item_ref",
                "source_container_ref",
                "destination_container_ref",
                "quantity",
                "origin",
            ),
            required=("item_ref", "destination_container_ref"),
        )
        quantity = read_count(command, "quantity", default=1, minimum=1, maximum=_QUANTITY_CEILING)
        if quantity > MAX_TRANSFER_QUANTITY:
            raise PreconditionFailed(
                f"quantity must be {MAX_TRANSFER_QUANTITY}: an item reference names one item, "
                "so moving several is several commands, each with its own evidence",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        source = (
            None
            if command.args.get("source_container_ref") is None
            else read_ref(command, "source_container_ref", kind=RefKind.CONTAINER)
        )
        return cls(
            item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM),
            source_ref=source,
            destination_ref=read_ref(command, "destination_container_ref", kind=RefKind.CONTAINER),
        )


@dataclass(frozen=True, slots=True)
class TransferAdapter:
    """``inventory.transfer``: move one item into a container that can hold it."""

    timeout_ms: int = DEFAULT_TRANSFER_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_TRANSFER_POLL_MS

    name: ClassVar[ActionName] = ActionName.INVENTORY_TRANSFER
    risk: ClassVar[RiskClass] = RiskClass.P1
    required_capability: ClassVar[str | None] = INVENTORY_TRANSFER

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _TransferSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        if spec.source_ref is not None and spec.source_ref != item.container_ref:
            raise PreconditionFailed(
                f"{item.display_name} is in {item.container_ref}, not in the named source",
                reason_code=ReasonCode.INVALID_REF,
                evidence={"item_ref": item.ref, "observed_container_ref": item.container_ref},
            )
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        destination = resolve_container(
            inventory, spec.destination_ref, field_name="destination_container_ref"
        )
        if source.ref == destination.ref:
            raise PreconditionFailed(
                f"{item.display_name} is already in {destination.name}",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "container_ref": destination.ref},
            )
        _check_equipped(item, observation)
        _check_reachable(
            container_chain(inventory, source), observation, field_name="source_container_ref"
        )
        _check_reachable(
            container_chain(inventory, destination),
            observation,
            field_name="destination_container_ref",
        )
        _check_not_into_itself(inventory, item, destination)
        _check_capacity(destination, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        spec = _TransferSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        return {
            "item_ref": item.ref,
            "source_container_ref": source.ref,
            "destination_container_ref": spec.destination_ref,
            "quantity": 1,
            # Carried so a caller — or a later undo — can put the item back
            # without having to have kept the pre-move observation (§4.6).
            "origin": describe_container(source),
        }

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _TransferSpec.parse(command)
        identity = identity_of(spec.item_ref)
        landed = _verify_landed(identity, spec.destination_ref, after)
        if landed is None or after.inventory is None:
            return None
        destination = after.inventory.container(spec.destination_ref)
        if destination is None:
            return None
        origin = command.args.get("origin")
        return _transfer_evidence(
            "item_in_destination_container",
            identity=identity,
            origin=dict(origin) if isinstance(origin, dict) else {},
            destination=destination,
            moved=landed,
            after=after,
        )

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """P1 while both ends travel with the character, P3 once the world is involved."""
        spec = _TransferSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        destination = resolve_container(
            inventory, spec.destination_ref, field_name="destination_container_ref"
        )
        if container_kind_is_world(source) or container_kind_is_world(destination):
            return RiskClass.P3
        return self.risk


def _ensure_main_item_ref(command: Command) -> str:
    """The one argument ``ensure_main`` takes, read the same way three times.

    The prepared command also carries the destination and the origin that
    ``build_args`` filled in, so they are accepted here: ``verify`` re-reads the
    command as it was *shipped*, and an argument check that refused the
    adapter's own output would fail every verification.
    """
    check_args(
        command,
        allowed=("item_ref", "destination_container_ref", "origin"),
        required=("item_ref",),
    )
    return read_ref(command, "item_ref", kind=RefKind.ITEM)


@dataclass(frozen=True, slots=True)
class EnsureMainAdapter:
    """``inventory.ensure_main``: get one item into the main inventory.

    An item already there is not special-cased into a fabricated success: the
    command still ships and the postcondition is observed on the next
    observation, exactly like any other attempt. A "no-op success" that skipped
    the observation would be the one result in the package asserted rather than
    seen.
    """

    timeout_ms: int = DEFAULT_ENSURE_MAIN_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_TRANSFER_POLL_MS

    name: ClassVar[ActionName] = ActionName.INVENTORY_ENSURE_MAIN
    risk: ClassVar[RiskClass] = RiskClass.P1
    required_capability: ClassVar[str | None] = INVENTORY_TRANSFER

    def validate(self, command: Command, observation: Observation) -> None:
        item_ref = _ensure_main_item_ref(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, item_ref)
        main = player_main(inventory)
        if item.container_ref == main.ref:
            return
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        _check_equipped(item, observation)
        _check_reachable(
            container_chain(inventory, source), observation, field_name="source_container_ref"
        )
        _check_capacity(main, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        item_ref = _ensure_main_item_ref(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, item_ref)
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        return {
            "item_ref": item.ref,
            "destination_container_ref": player_main(inventory).ref,
            "origin": describe_container(source),
        }

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        identity = identity_of(_ensure_main_item_ref(command))
        if after.inventory is None:
            return None
        main = after.inventory.main_container()
        if main is None:
            return None
        landed = _verify_landed(identity, main.ref, after)
        if landed is None:
            return None
        origin = command.args.get("origin")
        return _transfer_evidence(
            "item_in_player_main",
            identity=identity,
            origin=dict(origin) if isinstance(origin, dict) else {},
            destination=main,
            moved=landed,
            after=after,
        )

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        inventory = require_inventory(observation)
        item = resolve_item(inventory, _ensure_main_item_ref(command))
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        return RiskClass.P3 if container_kind_is_world(source) else self.risk
