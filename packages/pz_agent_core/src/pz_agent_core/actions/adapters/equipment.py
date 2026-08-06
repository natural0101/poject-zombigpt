"""``equipment.equip`` and ``equipment.unequip``: what the character holds and wears.

The postcondition is read off the character, never off the command: after an
equip, the requested slot holds *this* item; after an unequip, no slot holds it
and it is still somewhere on the character. The second half of that pair is what
makes unequip safe to report — an item that left the hand and also left the
inventory was dropped, and a dropped item is not an unequipped one (§5.12).

Identity is matched by runtime id rather than by reference string. Equipping
moves the item into the main inventory first, so its reference is re-minted on
the way, and a string comparison would report a perfectly good equip as a
failure while quietly matching a different object after a save/load boundary.

One observability limit is stated here rather than hidden in the code, because
it decides what this adapter can ever claim. The observer describes worn items
only when they are *containers* — a backpack has a reference, a t-shirt has
none. Wearing a garment with no inventory therefore leaves nothing in the
observation to point at, and the honest outcome is no evidence and a
postcondition failure, not a success inferred from the ack. A hand, by contrast,
is always reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import EQUIPMENT_EQUIP, EQUIPMENT_UNEQUIP
from ...protocol import (
    ActionName,
    Command,
    ContainerKind,
    ContainerRef,
    Hands,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    ItemIdentity,
    check_args,
    container_chain,
    find_by_identity,
    identity_of,
    read_ref,
    refused,
    require_inventory,
    resolve_item,
)
from .consume import require_in_main

__all__ = [
    "DEFAULT_EQUIP_POLL_MS",
    "DEFAULT_EQUIP_TIMEOUT_MS",
    "HANDS",
    "MAX_SLOT_NAME_LEN",
    "EquipAdapter",
    "UnequipAdapter",
    "worn_slot_of",
]

#: The hands a command may name. ``both`` is the two-handed case: the game
#: reports it as the primary hand holding the item, so that is what is verified.
HANDS: Final[frozenset[str]] = frozenset({"primary", "secondary", "both"})

#: Hands a single unequip may empty. ``both`` is absent on purpose — a hand is
#: emptied one at a time, and "take off both" is two commands with two results.
_UNEQUIP_HANDS: Final[frozenset[str]] = frozenset({"primary", "secondary"})

#: A body location as the engine spells it — ``Torso``, ``Jacket``, ``Back``.
MAX_SLOT_NAME_LEN: Final = 64

DEFAULT_EQUIP_TIMEOUT_MS: Final = 15_000
DEFAULT_EQUIP_POLL_MS: Final = 200


def _read_choice(command: Command, key: str, allowed: frozenset[str]) -> str | None:
    """Read a closed-vocabulary argument, or None when it was not given."""
    raw = command.args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in allowed:
        raise PreconditionFailed(
            f"{key} must be one of {sorted(allowed)}",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return raw


def _read_slot(command: Command) -> str | None:
    """Read a body-location name, or None when it was not given.

    Bounded and alphabet-checked: the value arrives from a model and is compared
    against a container reference tail, where a colon would change which segment
    it is being matched against.
    """
    raw = command.args.get("slot")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise PreconditionFailed(
            "slot must be a non-empty body location", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    if len(raw) > MAX_SLOT_NAME_LEN:
        raise PreconditionFailed(
            f"slot must be at most {MAX_SLOT_NAME_LEN} characters",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if not all(character.isalnum() or character in "._-" for character in raw):
        raise PreconditionFailed(
            "slot may only hold letters, digits, '.', '_' and '-'",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return raw


def _hand_identity(hands: Hands, hand: str) -> ItemIdentity | None:
    """What the named hand holds, as an identity, or None when it is empty."""
    ref = hands.secondary if hand == "secondary" else hands.primary
    if ref is None:
        return None
    try:
        return identity_of(ref, field_name=f"hands.{hand}")
    except PreconditionFailed:
        # A reference this side cannot parse can only ever fail to match, and a
        # malformed hand must not stop the worn slots from being checked.
        return None


def _worn_parts(container_ref: str) -> tuple[str, str] | None:
    """``(slot, runtime id)`` of a worn container, or None when it is not one.

    A reference the mod sent that this side cannot parse is dropped rather than
    raised on: it can only ever fail to match, and one malformed entry must not
    blind the search for the others.
    """
    try:
        parsed = ContainerRef.parse(container_ref)
    except RefError:
        return None
    parts = parsed.tail.split(":")
    if len(parts) != 3 or parts[0] != "worn":
        return None
    return parts[1], parts[2]


def worn_slot_of(inventory: InventoryView, identity: ItemIdentity) -> str | None:
    """The slot this item is worn in, or None when nothing reports it worn.

    Worn containers carry the item's runtime id in their own reference
    (``container:<session>:worn:<slot>:<runtime id>``), which is the only place
    the observation states where a garment sits.
    """
    for container in inventory.containers:
        if container.kind is not ContainerKind.WORN:
            continue
        parts = _worn_parts(container.ref)
        if parts is not None and parts[1] == identity.runtime_id:
            return parts[0]
    return None


def _still_carried(inventory: InventoryView, identity: ItemIdentity) -> ItemView | None:
    """The one item with this identity in an on-person container, or None.

    More than one match is not a partial success to wait out: two entries with
    one runtime id mean the observation cannot say which is "the" item, and an
    unequip that duplicated something must never be reported as one that worked.
    """
    matches = find_by_identity(inventory, identity)
    if len(matches) != 1:
        return None
    item = matches[0]
    container = inventory.container(item.container_ref)
    if container is None or not container_chain(inventory, container).on_person:
        return None
    return item


@dataclass(frozen=True, slots=True)
class _EquipSpec:
    """``item_ref`` plus the hand the caller asked for, if any.

    ``hand`` stays optional through to the wire because the mod decides between
    a hand and a body slot from the item itself — an item that reports a body
    location is worn, anything else goes in a hand. Defaulting it here would
    turn "equip this" into "put this in the primary hand" and refuse every
    garment.
    """

    item_ref: str
    hand: str | None

    @classmethod
    def parse(cls, command: Command) -> _EquipSpec:
        check_args(command, allowed=("item_ref", "hand"), required=("item_ref",))
        return cls(
            item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM),
            hand=_read_choice(command, "hand", HANDS),
        )

    def as_args(self) -> JsonDict:
        args: JsonDict = {"item_ref": self.item_ref}
        if self.hand is not None:
            args["hand"] = self.hand
        return args

    @property
    def verified_hand(self) -> str:
        """The hand a successful equip must be observed in.

        ``both`` verifies against the primary hand: the game reports a
        two-handed weapon as held in the primary hand, and requiring the
        secondary as well would fail an equip that did exactly what was asked.
        """
        return "secondary" if self.hand == "secondary" else "primary"


@dataclass(frozen=True, slots=True)
class EquipAdapter:
    """``equipment.equip``: put one item in a hand or on the body.

    Refuses an item that is already where it was asked to go. That is not
    tidiness: the postcondition would already hold, so the attempt could not
    distinguish "the equip worked" from "nothing happened", and reporting the
    first would be asserting a success rather than observing one.
    """

    timeout_ms: int = DEFAULT_EQUIP_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_EQUIP_POLL_MS

    name: ClassVar[ActionName] = ActionName.EQUIPMENT_EQUIP
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = EQUIPMENT_EQUIP

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _EquipSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        identity = identity_of(spec.item_ref)
        slot = worn_slot_of(inventory, identity)
        if slot is not None:
            raise PreconditionFailed(
                f"{item.display_name} is already worn in the {slot} slot",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "slot": slot},
            )
        held = _hand_identity(observation.player.hands, spec.verified_hand)
        if held == identity:
            raise PreconditionFailed(
                f"{item.display_name} is already in the {spec.verified_hand} hand",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "hand": spec.verified_hand},
            )
        # Both equip actions take the item out of the character's own main
        # inventory, so the transfer is declared as the prerequisite it is
        # rather than performed behind this command's id (§4.7).
        require_in_main(inventory, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _EquipSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _EquipSpec.parse(command)
        identity = identity_of(spec.item_ref)
        hand = spec.verified_hand
        if _hand_identity(after.player.hands, hand) == identity:
            return Evidence(
                kind="item_in_hand",
                observation_seq=after.seq,
                observed={
                    "item_ref": spec.item_ref,
                    "runtime_id": identity.runtime_id,
                    "slot": hand,
                    "requested_hand": spec.hand,
                    "primary_after": after.player.hands.primary,
                    "secondary_after": after.player.hands.secondary,
                },
            )
        if spec.hand is not None or after.inventory is None:
            # A command that named a hand asked for that hand. Accepting a body
            # slot instead would report success for an action nobody requested.
            return None
        slot = worn_slot_of(after.inventory, identity)
        if slot is None:
            return None
        return Evidence(
            kind="item_worn",
            observation_seq=after.seq,
            observed={
                "item_ref": spec.item_ref,
                "runtime_id": identity.runtime_id,
                "slot": slot,
                "requested_hand": None,
            },
        )


@dataclass(frozen=True, slots=True)
class _UnequipSpec:
    """Which of the three namings the caller used, and nothing more.

    Exactly one is accepted. An item reference resolves for anything in a
    container, but a worn garment lives in no container of its own and a held
    weapon may be reported in neither, so ``hand`` and ``slot`` name those
    directly. Two namings at once are refused rather than reconciled: they can
    disagree, and there is no defensible rule for which one wins.
    """

    item_ref: str | None
    hand: str | None
    slot: str | None

    @classmethod
    def parse(cls, command: Command) -> _UnequipSpec:
        check_args(command, allowed=("item_ref", "hand", "slot"))
        given = [key for key in ("item_ref", "hand", "slot") if command.args.get(key) is not None]
        if not given:
            raise PreconditionFailed(
                "name what to take off: item_ref, hand or slot",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        if len(given) > 1:
            raise PreconditionFailed(
                f"name what to take off exactly once, got {given}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        return cls(
            item_ref=(
                None
                if command.args.get("item_ref") is None
                else read_ref(command, "item_ref", kind=RefKind.ITEM)
            ),
            hand=_read_choice(command, "hand", _UNEQUIP_HANDS),
            slot=_read_slot(command),
        )

    def as_args(self) -> JsonDict:
        """The one key the caller used, passed through unchanged.

        Deliberately not rewritten into an ``item_ref``: a held weapon may sit
        in no container the mod can resolve a reference against, so replacing
        "the primary hand" with a reference would turn a command the mod can
        run into one it refuses.
        """
        if self.item_ref is not None:
            return {"item_ref": self.item_ref}
        if self.hand is not None:
            return {"hand": self.hand}
        return {"slot": self.slot}

    def identity_in(self, observation: Observation) -> ItemIdentity | None:
        """Which object this command names, as observed at the time.

        Resolved against an observation rather than kept from ``validate``
        because ``verify`` has to know what it is looking for and only ever sees
        the command: the *before* observation is where a hand or a slot still
        says what it held.
        """
        if self.item_ref is not None:
            return identity_of(self.item_ref)
        if self.hand is not None:
            return _hand_identity(observation.player.hands, self.hand)
        if observation.inventory is None:
            return None
        for container in observation.inventory.containers:
            if container.kind is not ContainerKind.WORN:
                continue
            parts = _worn_parts(container.ref)
            if parts is not None and parts[0] == self.slot:
                # A worn container's reference carries no generation, so the
                # identity is rebuilt at generation 0 — the generation the mod
                # mints every reference with today, and the only value a match
                # against an item reference could succeed on.
                return ItemIdentity(runtime_id=parts[1], generation=0)
        return None


@dataclass(frozen=True, slots=True)
class UnequipAdapter:
    """``equipment.unequip``: take one item off, and keep it.

    The two halves of the postcondition are checked together because either
    alone is satisfied by an outcome nobody wants: "no longer in a slot" is true
    of an item that fell on the floor, and "still in the inventory" is true of
    one that never came off.
    """

    timeout_ms: int = DEFAULT_EQUIP_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_EQUIP_POLL_MS

    name: ClassVar[ActionName] = ActionName.EQUIPMENT_UNEQUIP
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = EQUIPMENT_UNEQUIP

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _UnequipSpec.parse(command)
        inventory = require_inventory(observation)
        identity = spec.identity_in(observation)
        if identity is None:
            raise PreconditionFailed(
                f"nothing is equipped in {spec.hand or spec.slot}",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"hand": spec.hand, "slot": spec.slot},
            )
        hands = observation.player.hands
        in_hand = identity in (
            _hand_identity(hands, "primary"),
            _hand_identity(hands, "secondary"),
        )
        worn = worn_slot_of(inventory, identity)
        if not in_hand and worn is None:
            raise PreconditionFailed(
                "the item is neither held nor worn, so there is nothing to take off",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"runtime_id": identity.runtime_id},
            )
        if worn is not None or find_by_identity(inventory, identity):
            return
        # Held, and reported in no container: the item would be observable
        # afterwards only if the mod puts it back somewhere it describes, so
        # there is nothing here that could prove it was kept.
        raise refused(
            "the held item is not in any reported container, so an unequip "
            "could not be shown to have kept it",
            reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
            evidence={"runtime_id": identity.runtime_id},
        )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _UnequipSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _UnequipSpec.parse(command)
        identity = spec.identity_in(before)
        if identity is None or after.inventory is None:
            return None
        hands = after.player.hands
        if identity in (_hand_identity(hands, "primary"), _hand_identity(hands, "secondary")):
            return None
        slot = worn_slot_of(after.inventory, identity)
        if slot is not None:
            return None
        kept = _still_carried(after.inventory, identity)
        if kept is None:
            return None
        return Evidence(
            kind="item_unequipped",
            observation_seq=after.seq,
            observed={
                "item_ref": kept.ref,
                "runtime_id": identity.runtime_id,
                "container_ref": kept.container_ref,
                "display_name": kept.display_name,
                "hand": spec.hand,
                "slot": spec.slot,
                "primary_after": hands.primary,
                "secondary_after": hands.secondary,
            },
        )
