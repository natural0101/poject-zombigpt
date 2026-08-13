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
  ``equipment.unequip`` prerequisite instead (§5.12);
* a container is never put inside itself, however deep the nesting;
* a world container out of arm's reach yields a walk-to-it prerequisite rather
  than a transfer that would fail at the far end (§4.6).

``inventory.ensure_main`` is the same machinery with the destination fixed to
the main inventory. It is the preparation step every consumption and reading
action depends on (§4.7), which is why it is an action with its own command id
and its own evidence rather than something the eat adapter does on the side.

``inventory.transfer_batch`` is the loot-area version of the same move: up to
eight item references, each moved as one item by the game's own transfer, into
one destination. The single-transfer postcondition scales without weakening —
succeeded means *every* requested item is observed in the destination
afterwards, each by runtime id and counted exactly once. A batch the mod
stopped partway (capacity is re-checked before every item) is a failed terminal
whose ack carries the honest partial record; this side never upgrades a partial
landing into evidence, because a planner told "done" would loot on past items
still sitting in the crate.

``inventory.search`` sits alongside them and changes nothing at all. It is a
reading of what the character carries, and its postcondition is the one thing
worth guaranteeing about a list of references handed to a planner: every
reference in it resolves inside the character's *own* container tree. A search
that returned a reference into a crate across the room would look identical to
the caller and would produce a plan whose first step silently needs a walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities import INVENTORY_TRANSFER
from ...policy.drink import DrinkView
from ...policy.food import FoodView
from ...policy.literature import LiteratureView
from ...protocol import (
    ActionName,
    Command,
    ContainerView,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    belongs_to_session,
    ref_kind,
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
    read_flag,
    read_ref,
    refused,
    require_inventory,
    resolve_container,
    resolve_item,
    world_square,
)

__all__ = [
    "DEFAULT_BATCH_TIMEOUT_MS",
    "DEFAULT_ENSURE_MAIN_TIMEOUT_MS",
    "DEFAULT_SEARCH_POLL_MS",
    "DEFAULT_SEARCH_TIMEOUT_MS",
    "DEFAULT_TRANSFER_POLL_MS",
    "DEFAULT_TRANSFER_TIMEOUT_MS",
    "MAX_BATCH_ITEMS",
    "MAX_SEARCH_RESULTS",
    "MAX_TRANSFER_QUANTITY",
    "WORLD_REACH_SQUARES",
    "BatchTransferAdapter",
    "EnsureMainAdapter",
    "SearchAdapter",
    "TransferAdapter",
    "unequip_prerequisite",
]

#: One reference names one item, so one command moves one item. Bulk movement
#: under a single command id used to be forbidden outright for lack of an honest
#: terminal result; ``inventory.transfer_batch`` earned one by making a partial
#: batch a *failed* terminal whose evidence records exactly what landed, so this
#: rule keeps holding for the single transfer: its ``quantity`` stays 1.
MAX_TRANSFER_QUANTITY: Final = 1

#: Items one batch may name. The mod's dispatcher caps a declared list at the
#: same eight (``CommandDispatcher.MAX_LIST_ITEMS``), and the MCP schema
#: restates it; every item is verified individually in the destination, so a
#: wider batch is a longer list of claims one command id has to answer for.
MAX_BATCH_ITEMS: Final = 8

#: Squares within which a world container counts as reachable without walking.
WORLD_REACH_SQUARES: Final = 1

#: Upper bound the ``quantity`` argument is read against, so that a caller who
#: asks for ten gets the explanation rather than a range error.
_QUANTITY_CEILING: Final = 64

DEFAULT_TRANSFER_TIMEOUT_MS: Final = 10_000
DEFAULT_TRANSFER_POLL_MS: Final = 200

#: The batch runs up to :data:`MAX_BATCH_ITEMS` of the game's own transfer
#: actions back to back, so its budget is the single transfer's, scaled by the
#: batch bound — derived rather than invented, and still finite.
DEFAULT_BATCH_TIMEOUT_MS: Final = MAX_BATCH_ITEMS * DEFAULT_TRANSFER_TIMEOUT_MS

DEFAULT_ENSURE_MAIN_TIMEOUT_MS: Final = 15_000

#: References one search reports. The list travels in an ack and is read by a
#: planner with a bounded plan length, so a hundred hits would be a hundred
#: references nothing could act on.
MAX_SEARCH_RESULTS: Final = 32

#: A search is answered by the next observation; waiting longer would not make
#: the mod describe more of the inventory.
DEFAULT_SEARCH_TIMEOUT_MS: Final = 5_000
DEFAULT_SEARCH_POLL_MS: Final = 100

#: Longest type filter a search accepts. An item type is ``Base.Bandage``-sized;
#: anything longer is not a type this install has.
MAX_TYPE_FILTER_LEN: Final = 128


def unequip_prerequisite(item: ItemView) -> Prerequisite:
    """The preparation an equipped item needs before it can be moved."""
    return Prerequisite(
        action=ActionName.EQUIPMENT_UNEQUIP,
        args={"item_ref": item.ref},
        detail=f"{item.display_name} is equipped and must be put away first",
    )


def _approach_prerequisite(container: ContainerView, square: tuple[int, int, int]) -> Prerequisite:
    """Walk to the container's own square with an adjacency radius.

    ``move_to`` rather than ``move_near``, but not for the reason this said
    before: ``movement.move_near`` does accept a container reference
    (``_MoveNearSpec.parse``, and the mod declares the same three kinds), so
    "walk to the crate" is expressible. The prerequisite stays a ``move_to``
    because the square is derivable from the container reference here and a
    ``move_to`` carries the arrival radius explicitly; a reader weighing the
    two should know both run, and that neither runs today for the reason in
    docs/LIMITATIONS.md.
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


def _origin_from(before: Observation, identity: ItemIdentity) -> JsonDict:
    """Where the item stood before the move, read from the observation.

    This used to be handed to `verify` inside the command, put there by
    `build_args`. Reading it from `before` is not just the fix for an argument
    the mod refused — it is the better source. An origin the sidecar wrote down
    is an assertion about the world; an origin recovered from the observation
    the mod sent is a reading of it, and the difference is exactly the one this
    project draws everywhere else.

    An empty dict when the item cannot be found in `before`: an origin nobody
    observed is not a fact to put in evidence.
    """
    if before.inventory is None:
        return {}
    candidates = find_by_identity(before.inventory, identity)
    if len(candidates) != 1:
        return {}
    source = before.inventory.container(candidates[0].container_ref)
    if source is None:
        return {}
    return describe_container(source)


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
        }
        # `origin` used to travel here too, as a nested object describing the
        # source container, so a caller could undo the move without having kept
        # the pre-move observation. It could never arrive: the mod's dispatcher
        # accepts only scalar arguments and refuses an undeclared key outright,
        # so the whole transfer came back INVALID_ARGUMENT. `verify` now reads
        # the origin from the before-observation, which is a better source
        # anyway — it is observed rather than asserted.

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _TransferSpec.parse(command)
        identity = identity_of(spec.item_ref)
        landed = _verify_landed(identity, spec.destination_ref, after)
        if landed is None or after.inventory is None:
            return None
        destination = after.inventory.container(spec.destination_ref)
        if destination is None:
            return None
        return _transfer_evidence(
            "item_in_destination_container",
            identity=identity,
            origin=_origin_from(before, identity),
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


# --------------------------------------------------------------------------
# inventory.transfer_batch
# --------------------------------------------------------------------------


def _read_item_refs(command: Command) -> tuple[str, ...]:
    """Read the batch's item list, refusing by index so the caller can fix one.

    The checks mirror :func:`~.common.read_ref` element by element — same
    codes, same session rule — and the bounds mirror the mod's own list
    checker (``CommandDispatcher.checkList``): 1..:data:`MAX_BATCH_ITEMS`
    elements, dense, no duplicates. A duplicate is one object asked to move
    twice; the second copy could only succeed by lying about the first.
    """
    raw = command.args.get("item_refs")
    if not isinstance(raw, list):
        raise PreconditionFailed(
            "item_refs must be a list of item references",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if not raw:
        raise PreconditionFailed(
            "item_refs must name at least one item",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if len(raw) > MAX_BATCH_ITEMS:
        raise PreconditionFailed(
            f"item_refs may name at most {MAX_BATCH_ITEMS} items, got {len(raw)}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    refs: list[str] = []
    seen: dict[str, int] = {}
    for index, element in enumerate(raw):
        if not isinstance(element, str) or not element:
            raise PreconditionFailed(
                f"item_refs[{index}] must be a non-empty reference string",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        try:
            actual = ref_kind(element)
        except RefError as exc:
            raise PreconditionFailed(
                f"item_refs[{index}] is not a reference: {element!r}",
                reason_code=ReasonCode.INVALID_REF,
            ) from exc
        if actual is not RefKind.ITEM:
            raise PreconditionFailed(
                f"item_refs[{index}] must be an item reference, got a {actual.value} reference",
                reason_code=ReasonCode.INVALID_REF,
            )
        if not belongs_to_session(element, command.session_id):
            raise PreconditionFailed(
                f"item_refs[{index}] was minted by a different session",
                reason_code=ReasonCode.INVALID_REF,
            )
        earlier = seen.get(element)
        if earlier is not None:
            raise PreconditionFailed(
                f"item_refs[{index}] repeats item_refs[{earlier}]",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        seen[element] = index
        refs.append(element)
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class _BatchSpec:
    item_refs: tuple[str, ...]
    destination_ref: str

    @classmethod
    def parse(cls, command: Command) -> _BatchSpec:
        check_args(
            command,
            allowed=("item_refs", "destination_container_ref"),
            required=("item_refs", "destination_container_ref"),
        )
        return cls(
            item_refs=_read_item_refs(command),
            destination_ref=read_ref(command, "destination_container_ref", kind=RefKind.CONTAINER),
        )


def _resolve_batch(inventory: InventoryView, spec: _BatchSpec) -> tuple[ItemView, ...]:
    """Every named item, or the first ``INVALID_REF`` with its index named."""
    items: list[ItemView] = []
    for index, ref in enumerate(spec.item_refs):
        item = inventory.item(ref)
        if item is None:
            raise PreconditionFailed(
                f"item_refs[{index}] {ref} is not in the observed inventory",
                reason_code=ReasonCode.INVALID_REF,
                evidence={"item_ref": ref, "index": index},
            )
        items.append(item)
    return tuple(items)


def _check_batch_capacity(destination: ContainerView, items: tuple[ItemView, ...]) -> None:
    """Refuse a batch whose observed weights cannot all fit, naming the prefix.

    The mod re-checks capacity before every item and stops the batch at the
    first that would not fit, so a batch this check can already see failing
    would ship only to come back a partial failure. The refusal names how many
    items *would* fit — "the first k of n" — so the planner can resubmit the
    prefix instead of guessing. A destination whose capacity the game does not
    report is let through, exactly like the single transfer: the mod is the
    judge, and inventing a limit would refuse a move that would have worked.
    """
    free = destination.free_capacity
    if free is None:
        return
    total = 0.0
    fits = 0
    for item in items:
        total += item.weight
        if total <= free:
            fits += 1
    if total <= free:
        return
    raise PreconditionFailed(
        f"{destination.name} has {free:.2f} free and the batch weighs {total:.2f}; "
        f"the destination has room for the first {fits} of {len(items)}",
        reason_code=ReasonCode.CONTAINER_FULL,
        evidence={
            "destination_ref": destination.ref,
            "free_capacity": free,
            "batch_weight": total,
            "prefix_that_fits": fits,
            "requested": len(items),
        },
    )


def _destination_count(observation: Observation, destination_ref: str) -> int | None:
    """How many items the observation shows inside the destination, or None."""
    if observation.inventory is None:
        return None
    return sum(1 for item in observation.inventory.items if item.container_ref == destination_ref)


@dataclass(frozen=True, slots=True)
class BatchTransferAdapter:
    """``inventory.transfer_batch``: move up to eight named items into one container.

    Succeeded only when *every* requested item is observed in the destination
    afterwards — each matched by runtime id and counted exactly once, the same
    duplication rule the single transfer lives by. Any item missing means no
    evidence at all: the mod's failed ack is where a partial batch is recorded
    (what landed, what stopped, why), and ``verify`` refusing to bless the
    partial state is what keeps that record honest rather than a lie about
    full success.
    """

    timeout_ms: int = DEFAULT_BATCH_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_TRANSFER_POLL_MS

    name: ClassVar[ActionName] = ActionName.INVENTORY_TRANSFER_BATCH
    risk: ClassVar[RiskClass] = RiskClass.P1
    required_capability: ClassVar[str | None] = INVENTORY_TRANSFER

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _BatchSpec.parse(command)
        inventory = require_inventory(observation)
        destination = resolve_container(
            inventory, spec.destination_ref, field_name="destination_container_ref"
        )
        items = _resolve_batch(inventory, spec)
        for item in items:
            _check_equipped(item, observation)
        _check_reachable(
            container_chain(inventory, destination),
            observation,
            field_name="destination_container_ref",
        )
        # The items may live in different source containers; each distinct one
        # is checked for reach once, because the character stands in one place
        # for the whole batch and every source must be openable from there.
        checked_sources: set[str] = set()
        for item in items:
            if item.container_ref in checked_sources:
                continue
            checked_sources.add(item.container_ref)
            source = resolve_container(
                inventory, item.container_ref, field_name="source_container_ref"
            )
            _check_reachable(
                container_chain(inventory, source), observation, field_name="source_container_ref"
            )
        for item in items:
            _check_not_into_itself(inventory, item, destination)
        _check_batch_capacity(destination, items)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        # Called for the refusal, not the values: the payload is exactly the
        # declared shape, but a batch naming an item the observation does not
        # show must be refused here rather than at the far end of the wire.
        spec = _BatchSpec.parse(command)
        _resolve_batch(require_inventory(observation), spec)
        return {
            "item_refs": list(spec.item_refs),
            "destination_container_ref": spec.destination_ref,
        }

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _BatchSpec.parse(command)
        if after.inventory is None:
            return None
        if after.inventory.container(spec.destination_ref) is None:
            return None
        transferred: list[str] = []
        for ref in spec.item_refs:
            landed = _verify_landed(identity_of(ref), spec.destination_ref, after)
            if landed is None:
                # One item not (or twice) observed in the destination unproves
                # the whole batch; a partial landing is the mod's failed ack to
                # report, never this side's success.
                return None
            transferred.append(landed.ref)
        before_count = _destination_count(before, spec.destination_ref)
        after_count = _destination_count(after, spec.destination_ref)
        return Evidence(
            kind="items_in_destination_container",
            observation_seq=after.seq,
            observed={
                "destination_ref": spec.destination_ref,
                "requested": len(spec.item_refs),
                "transferred": transferred,
                # Every requested item was observed to land, so nothing
                # stopped; the key is still here because the evidence shape is
                # the contract's, shared with the mod's partial record.
                "stopped": [],
                # None when the before-observation had no inventory tier: a
                # delta nobody observed is not a fact to put in evidence.
                "destination_count_delta": (
                    None
                    if before_count is None or after_count is None
                    else after_count - before_count
                ),
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        """Items observed to have landed, over items requested."""
        spec = _BatchSpec.parse(command)
        if latest.inventory is None:
            return None
        landed = sum(
            1
            for ref in spec.item_refs
            if _verify_landed(identity_of(ref), spec.destination_ref, latest) is not None
        )
        return landed / len(spec.item_refs)

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """P1 while every endpoint travels with the character, P3 once the world is involved."""
        spec = _BatchSpec.parse(command)
        inventory = require_inventory(observation)
        destination = resolve_container(
            inventory, spec.destination_ref, field_name="destination_container_ref"
        )
        if container_kind_is_world(destination):
            return RiskClass.P3
        for item in _resolve_batch(inventory, spec):
            source = resolve_container(
                inventory, item.container_ref, field_name="source_container_ref"
            )
            if container_kind_is_world(source):
                return RiskClass.P3
        return self.risk


def _ensure_main_item_ref(command: Command) -> str:
    """The one argument ``ensure_main`` takes, read the same way three times.

    The prepared command also carries the destination ``build_args`` filled
    in, so it is accepted here: ``verify`` re-reads the command as it was
    *shipped*, and an argument check that refused the adapter's own output would
    fail every verification.
    """
    check_args(
        command,
        allowed=("item_ref", "destination_container_ref"),
        required=("item_ref",),
    )
    return read_ref(command, "item_ref", kind=RefKind.ITEM)


def _check_declared_destination(command: Command, main: ContainerView) -> None:
    """Refuse a ``destination_container_ref`` that is not the main inventory.

    ``build_args`` fills the argument in, so the prepared command carries it and
    it has to be accepted — but a caller who supplies a *different* container is
    asking for ``inventory.transfer``, not for this action. Silently rewriting
    it would run an action nobody requested and then verify the one that ran.
    """
    if command.args.get("destination_container_ref") is None:
        return
    declared = read_ref(command, "destination_container_ref", kind=RefKind.CONTAINER)
    if declared != main.ref:
        raise PreconditionFailed(
            f"inventory.ensure_main always targets {main.ref}; "
            f"moving an item to {declared} is inventory.transfer",
            reason_code=ReasonCode.INVALID_ARGUMENT,
            evidence={"destination_container_ref": declared, "player_main_ref": main.ref},
        )


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
        _check_declared_destination(command, main)
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
        # Called for the refusal, not the value: an item whose container the
        # observation does not describe is one the mod cannot be asked to move,
        # and finding that out here costs a rejected command rather than a
        # transfer that fails at the far end. `verify` recovers the origin from
        # the before-observation, so nothing needs the container itself.
        resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        return {
            "item_ref": item.ref,
            "destination_container_ref": player_main(inventory).ref,
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
        return _transfer_evidence(
            "item_in_player_main",
            identity=identity,
            origin=_origin_from(before, identity),
            destination=main,
            moved=landed,
            after=after,
        )

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        inventory = require_inventory(observation)
        item = resolve_item(inventory, _ensure_main_item_ref(command))
        source = resolve_container(inventory, item.container_ref, field_name="source_container_ref")
        return RiskClass.P3 if container_kind_is_world(source) else self.risk


# --------------------------------------------------------------------------
# inventory.search
# --------------------------------------------------------------------------


def _read_type_text(command: Command, key: str) -> str | None:
    """Read one of the two type filters, or None when it was not given.

    Bounded and alphabet-checked because the value arrives from a model and is
    matched against game data: an item type is ``Base.Bandage``-shaped, and
    anything carrying a space, a colon or a wildcard is a pattern this adapter
    does not implement rather than a type it failed to find.
    """
    raw = command.args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise PreconditionFailed(
            f"{key} must be a non-empty string", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    if len(raw) > MAX_TYPE_FILTER_LEN:
        raise PreconditionFailed(
            f"{key} must be at most {MAX_TYPE_FILTER_LEN} characters",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if not all(character.isalnum() or character in "._-" for character in raw):
        raise PreconditionFailed(
            f"{key} may only hold letters, digits, '.', '_' and '-'",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return raw


def _read_tristate(command: Command, key: str) -> bool | None:
    """A flag that may also be absent.

    ``None`` is "do not filter on this", which is a different request from
    ``false`` ("must not be edible"). Collapsing the two would silently widen
    every search that left a filter out.
    """
    if command.args.get(key) is None:
        return None
    return read_flag(command, key, default=False)


@dataclass(frozen=True, slots=True)
class _SearchSpec:
    """The filter one search applies, normalised.

    ``min_uses`` and ``name_contains`` are deliberately not here. The mod
    supports both; the observation carries neither a use counter nor free item
    text, so a result filtered on them could not be checked against anything
    this side sees. An argument that cannot be verified is refused rather than
    forwarded — the alternative is a search whose report is taken on trust.
    """

    full_type: str | None
    type_prefix: str | None
    edible: bool | None
    drinkable: bool | None
    readable: bool | None
    exclude_equipped: bool
    limit: int

    @classmethod
    def parse(cls, command: Command) -> _SearchSpec:
        check_args(
            command,
            allowed=(
                "full_type",
                "type_prefix",
                "edible",
                "drinkable",
                "readable",
                "exclude_equipped",
                "limit",
            ),
        )
        return cls(
            full_type=_read_type_text(command, "full_type"),
            type_prefix=_read_type_text(command, "type_prefix"),
            edible=_read_tristate(command, "edible"),
            drinkable=_read_tristate(command, "drinkable"),
            readable=_read_tristate(command, "readable"),
            exclude_equipped=read_flag(command, "exclude_equipped", default=False),
            limit=read_count(
                command, "limit", default=MAX_SEARCH_RESULTS, minimum=1, maximum=MAX_SEARCH_RESULTS
            ),
        )

    def as_args(self) -> JsonDict:
        args: JsonDict = {"exclude_equipped": self.exclude_equipped, "limit": self.limit}
        for key, value in (
            ("full_type", self.full_type),
            ("type_prefix", self.type_prefix),
            ("edible", self.edible),
            ("drinkable", self.drinkable),
            ("readable", self.readable),
        ):
            if value is not None:
                args[key] = value
        return args

    def matches(self, item: ItemView, observation: Observation) -> bool:
        if self.full_type is not None and item.full_type != self.full_type:
            return False
        if self.type_prefix is not None and not item.full_type.startswith(self.type_prefix):
            return False
        if self.exclude_equipped and (item.equipped or observation.player.hands.holds(item.ref)):
            return False
        if self.edible is not None and self.edible is not _is_edible(item):
            return False
        if self.drinkable is not None and self.drinkable is not _is_drinkable(item):
            return False
        readable = LiteratureView.from_item(item) is not None
        return self.readable is None or self.readable is readable


def _is_edible(item: ItemView) -> bool:
    """Whether the game itself would let the character eat this.

    Read through :class:`~pz_agent_core.policy.food.FoodView` rather than by
    looking for a ``food`` key, so "edible" means here exactly what it means to
    the policy that will later be asked to choose between the results.
    """
    view = FoodView.from_item(item)
    return view is not None and view.edible and not view.destroyed


def _is_drinkable(item: ItemView) -> bool:
    view = DrinkView.from_item(item)
    return view is not None and view.drinkable and not view.destroyed


@dataclass(frozen=True, slots=True)
class SearchAdapter:
    """``inventory.search``: list what the character is carrying that matches.

    Read-only: it queues nothing and needs no arming, so it names no
    ``required_capability`` — what it needs is the observation's inventory tier,
    and :func:`require_inventory` refuses when the mod did not produce one.

    The postcondition is not "the mod answered". It is that every reference the
    report carries resolves to an item inside the character's own container
    tree, checked chain by chain against the observation the report was built
    from. An item in a world container can therefore never appear in a search
    result, which is what lets a planner treat these references as things it can
    act on without walking anywhere first.
    """

    timeout_ms: int = DEFAULT_SEARCH_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_SEARCH_POLL_MS

    name: ClassVar[ActionName] = ActionName.INVENTORY_SEARCH
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        _SearchSpec.parse(command)
        require_inventory(observation)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _SearchSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _SearchSpec.parse(command)
        inventory = after.inventory
        if inventory is None:
            return None
        matches: list[JsonDict] = []
        off_person = 0
        for item in inventory.items:
            if len(matches) >= spec.limit:
                break
            if not spec.matches(item, after):
                continue
            container = inventory.container(item.container_ref)
            if container is None or not container_chain(inventory, container).on_person:
                # The filter is not what keeps a crate out of the results; this
                # is. A reference the character cannot reach without walking is
                # dropped and counted, never quietly listed.
                off_person += 1
                continue
            matches.append(
                {
                    "item_ref": item.ref,
                    "container_ref": item.container_ref,
                    "full_type": item.full_type,
                    "display_name": item.display_name,
                    "weight": item.weight,
                    "equipped": item.equipped,
                }
            )
        return Evidence(
            kind="inventory_searched",
            observation_seq=after.seq,
            observed={
                "matches": matches,
                "match_count": len(matches),
                "limit": spec.limit,
                "off_person_skipped": off_person,
                "items_examined": len(inventory.items),
            },
        )
