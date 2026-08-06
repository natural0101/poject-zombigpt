"""``consume.eat`` and ``consume.drink``: spending an item on a need.

These adapters *execute* a choice; they never make one. Which tin and which
bottle is decided by :mod:`pz_agent_core.policy.food` and
:mod:`pz_agent_core.policy.drink`, deterministically and with a score
breakdown, and duplicating any of that here would create a second copy of the
safety filters that no test compares against the first. What is checked here is
only what would make the action itself impossible: the item is of the right
kind, the game considers it consumable, there is something left in it, and it
is in the character's hands rather than at the bottom of a rucksack.

The postcondition is deliberately a disjunction (§4.8, §4.9). Hunger and thirst
are the needs the action exists to serve, but a single bite of a large item can
round to no visible change in the stat while the item's own counter moves; and
an item eaten to nothing simply vanishes, which §4.8 says explicitly is not a
failure. Any one of the three is proof; none of them is assumed.

Drinking from a sink, a well or a rain collector is a *third* adapter,
:class:`DrinkSourceAdapter`, behind the ``drink_world_source`` probe. §4.9 asks
for the split and §12.4 explains why it has to be structural: that probe is
capped at ``experimental`` on a clean scan, and while the world source was an
optional argument on ``consume.drink`` the whole path ran under
``drink_carried`` — an unproven capability reachable through a verified one.
One action, one capability, checked by the engine before an adapter is entered.

So ``consume.drink`` means "drink from a carried container" and nothing else,
and it refuses an argument naming a world source rather than honouring it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities import DRINK_CARRIED, DRINK_WORLD_SOURCE, EAT_PERCENTAGE
from ...policy.drink import DrinkView
from ...policy.food import FoodView
from ...protocol import (
    ActionName,
    Command,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
    PlayerState,
    ReasonCode,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    ItemIdentity,
    Prerequisite,
    check_args,
    find_by_identity,
    identity_of,
    nearby_object,
    player_main,
    read_number,
    read_ref,
    refused,
    require_inventory,
    require_nearby,
    resolve_item,
)

__all__ = [
    "DEFAULT_CONSUME_POLL_MS",
    "DEFAULT_DRINK_TIMEOUT_MS",
    "DEFAULT_EAT_TIMEOUT_MS",
    "MIN_CONSUME_FRACTION",
    "DrinkAdapter",
    "DrinkSourceAdapter",
    "EatAdapter",
    "ensure_main_prerequisite",
    "require_in_main",
]

#: Smaller than this and the action is not worth a command: the stat change
#: would sit inside the noise the postcondition has to see through.
MIN_CONSUME_FRACTION: Final = 0.05

DEFAULT_EAT_TIMEOUT_MS: Final = 30_000
DEFAULT_DRINK_TIMEOUT_MS: Final = 20_000
DEFAULT_CONSUME_POLL_MS: Final = 250


def ensure_main_prerequisite(item: ItemView) -> Prerequisite:
    """The transfer a consumption or reading action depends on (§4.7).

    Returned as a refusal rather than performed inline: the dependency is real,
    and a command that quietly moved the item first would report one result for
    two actions, leaving "the transfer failed" and "the meal failed"
    indistinguishable.
    """
    return Prerequisite(
        action=ActionName.INVENTORY_ENSURE_MAIN,
        args={"item_ref": item.ref},
        detail=(
            f"{item.display_name} is in {item.container_ref} and has to be in the main "
            "inventory before it can be used"
        ),
    )


def require_in_main(inventory: InventoryView, item: ItemView) -> None:
    """Refuse an item that is not in the main inventory, naming the fix."""
    main = player_main(inventory)
    if item.container_ref == main.ref:
        return
    raise refused(
        f"{item.display_name} is not in the main inventory",
        reason_code=ReasonCode.PRECONDITION_FAILED,
        prerequisites=(ensure_main_prerequisite(item),),
        evidence={"item_ref": item.ref, "container_ref": item.container_ref},
    )


@dataclass(frozen=True, slots=True)
class _ConsumeSpec:
    """``item_ref`` plus how much of it to consume."""

    item_ref: str
    fraction: float

    @classmethod
    def parse(cls, command: Command) -> _ConsumeSpec:
        check_args(command, allowed=("item_ref", "fraction"), required=("item_ref",))
        return cls(
            item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM),
            fraction=read_number(
                command, "fraction", default=1.0, minimum=MIN_CONSUME_FRACTION, maximum=1.0
            ),
        )

    def as_args(self) -> JsonDict:
        return {"item_ref": self.item_ref, "fraction": self.fraction}


@dataclass(frozen=True, slots=True)
class _Consumed:
    """What the two observations say happened to the item itself."""

    present_before: bool
    present_after: bool
    amount_before: float | None
    amount_after: float | None

    @property
    def vanished(self) -> bool:
        """True when an item that was there is gone — §4.8's "eaten entirely"."""
        return self.present_before and not self.present_after

    @property
    def decreased(self) -> bool:
        if self.amount_before is None or self.amount_after is None:
            return False
        return self.amount_after < self.amount_before

    def as_dict(self, *, amount_key: str) -> JsonDict:
        return {
            f"{amount_key}_before": self.amount_before,
            f"{amount_key}_after": self.amount_after,
            "item_present_after": self.present_after,
        }


def _amount(item: ItemView | None, *, drink: bool) -> float | None:
    """The item's own remaining-quantity counter, or None when unreadable."""
    if item is None:
        return None
    if drink:
        view = DrinkView.from_item(item)
        return None if view is None else view.remaining_units
    food = FoodView.from_item(item)
    return None if food is None else float(food.remaining_portions)


def _track(
    identity: ItemIdentity, before: Observation, after: Observation, *, drink: bool
) -> _Consumed | None:
    """Follow one item across two observations, or None when it cannot be followed.

    Both observations must carry an inventory: an item "missing" from an
    observation that simply has no inventory tier is not evidence that it was
    eaten, and treating it as such is the exact shape of a fabricated success.

    Two entries with one runtime id are not followable either. "Which of them
    was the one bitten into" has no answer, so the item side of the
    postcondition is dropped and only the stat can carry the proof.
    """
    if before.inventory is None or after.inventory is None:
        return None
    was = find_by_identity(before.inventory, identity)
    now = find_by_identity(after.inventory, identity)
    if len(was) > 1 or len(now) > 1:
        return None
    return _Consumed(
        present_before=bool(was),
        present_after=bool(now),
        amount_before=_amount(was[0] if was else None, drink=drink),
        amount_after=_amount(now[0] if now else None, drink=drink),
    )


def _need_evidence(
    kind: str,
    *,
    stat: str,
    before_value: float,
    after_value: float,
    consumed: _Consumed | None,
    amount_key: str,
    spec: _ConsumeSpec,
    after: Observation,
) -> Evidence:
    observed: JsonDict = {
        "item_ref": spec.item_ref,
        "fraction": spec.fraction,
        f"{stat}_before": before_value,
        f"{stat}_after": after_value,
    }
    if consumed is not None:
        observed.update(consumed.as_dict(amount_key=amount_key))
    return Evidence(kind=kind, observation_seq=after.seq, observed=observed)


def _stat_value(player: PlayerState, *, drink: bool) -> float:
    return player.thirst if drink else player.hunger


def _verify(
    spec: _ConsumeSpec,
    before: Observation,
    after: Observation,
    *,
    drink: bool,
    stat: str,
    amount_key: str,
) -> Evidence | None:
    """Shared postcondition: the need fell, or the item did.

    Order matters only for which evidence label is reported. The stat is looked
    at first because it is the thing the action was for; the item counter is
    the fallback for a bite too small to move it.
    """
    identity = identity_of(spec.item_ref)
    consumed = _track(identity, before, after, drink=drink)
    start = _stat_value(before.player, drink=drink)
    end = _stat_value(after.player, drink=drink)
    if end < start:
        return _need_evidence(
            f"{stat}_decreased",
            stat=stat,
            before_value=start,
            after_value=end,
            consumed=consumed,
            amount_key=amount_key,
            spec=spec,
            after=after,
        )
    if consumed is not None and (consumed.decreased or consumed.vanished):
        return _need_evidence(
            f"{amount_key}_decreased" if consumed.decreased else "item_consumed_entirely",
            stat=stat,
            before_value=start,
            after_value=end,
            consumed=consumed,
            amount_key=amount_key,
            spec=spec,
            after=after,
        )
    return None


@dataclass(frozen=True, slots=True)
class EatAdapter:
    """``consume.eat``: eat a chosen fraction of a chosen item."""

    timeout_ms: int = DEFAULT_EAT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CONSUME_POLL_MS

    name: ClassVar[ActionName] = ActionName.CONSUME_EAT
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = EAT_PERCENTAGE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _ConsumeSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        view = FoodView.from_item(item)
        if view is None:
            raise PreconditionFailed(
                f"{item.display_name} is not food",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "category": item.category},
            )
        # Deliberately not the safety filters: rot, poison and reserves are
        # policy.food's decision and are already made by the time a reference
        # reaches here. These two are about whether the action can run at all.
        if not view.edible or view.destroyed:
            raise PreconditionFailed(
                f"the game does not consider {item.display_name} edible",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "edible": view.edible},
            )
        if view.remaining_portions <= 0:
            raise PreconditionFailed(
                f"{item.display_name} has no portions left",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "remaining_portions": view.remaining_portions},
            )
        require_in_main(inventory, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _ConsumeSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        return _verify(
            _ConsumeSpec.parse(command),
            before,
            after,
            drink=False,
            stat="hunger",
            amount_key="portions",
        )


@dataclass(frozen=True, slots=True)
class DrinkAdapter:
    """``consume.drink``: drink from a carried container."""

    timeout_ms: int = DEFAULT_DRINK_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CONSUME_POLL_MS

    name: ClassVar[ActionName] = ActionName.CONSUME_DRINK
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = DRINK_CARRIED

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _ConsumeSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        view = DrinkView.from_item(item)
        if view is None:
            raise PreconditionFailed(
                f"{item.display_name} holds no fluid",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "category": item.category},
            )
        if not view.drinkable or view.destroyed:
            raise PreconditionFailed(
                f"the game does not consider {item.display_name} drinkable",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "drinkable": view.drinkable},
            )
        if view.remaining_units <= 0.0:
            raise PreconditionFailed(
                f"{item.display_name} is empty",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "remaining_units": view.remaining_units},
            )
        require_in_main(inventory, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _ConsumeSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        return _verify(
            _ConsumeSpec.parse(command),
            before,
            after,
            drink=True,
            stat="thirst",
            amount_key="volume",
        )


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    """A vessel to fill, how much of it to drink, and where the water is."""

    item_ref: str
    fraction: float
    source_ref: str

    @classmethod
    def parse(cls, command: Command) -> _SourceSpec:
        check_args(
            command,
            allowed=("item_ref", "fraction", "source_ref"),
            required=("item_ref", "source_ref"),
        )
        return cls(
            item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM),
            fraction=read_number(
                command, "fraction", default=1.0, minimum=MIN_CONSUME_FRACTION, maximum=1.0
            ),
            source_ref=read_ref(command, "source_ref", kind=RefKind.SQUARE),
        )

    def as_args(self) -> JsonDict:
        return {
            "item_ref": self.item_ref,
            "fraction": self.fraction,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class DrinkSourceAdapter:
    """``consume.drink_source``: fill a vessel at a world source, then drink.

    Two things make this a different action rather than an argument on
    :class:`DrinkAdapter`, and both are load-bearing.

    The first is the capability. ``drink_world_source`` is capped at
    ``experimental`` by its probe, so it is upgradeable but not usable until a
    live ack confirms it — and the engine reads ``required_capability`` from the
    adapter before validating anything. Folded into ``consume.drink`` the same
    work would have run under ``drink_carried``, which a static scan verifies.

    The second is the postcondition. A refill raises the vessel's contents and
    the drink lowers them again, so the item counter that carries
    ``consume.drink`` when a sip is too small to move the stat is not a witness
    here in either direction. Thirst is the only honest reading, and if this
    build does not report thirst the action cannot succeed at all — which is
    the correct outcome, not a gap.
    """

    timeout_ms: int = DEFAULT_DRINK_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CONSUME_POLL_MS

    name: ClassVar[ActionName] = ActionName.CONSUME_DRINK_SOURCE
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = DRINK_WORLD_SOURCE

    #: What the mod puts on a nearby object that reported a positive water
    #: amount (``PZAgent.Observe.objectFields``). Naming a square that does not
    #: carry it is refused here rather than in the mod, so the refusal costs no
    #: round trip.
    WATER_SEMANTIC: ClassVar[str] = "water_source"

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _SourceSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        view = DrinkView.from_item(item)
        if view is None:
            raise PreconditionFailed(
                f"{item.display_name} holds no fluid, so there is nothing to fill",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "category": item.category},
            )
        if view.destroyed:
            raise PreconditionFailed(
                f"{item.display_name} is destroyed",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref, "destroyed": view.destroyed},
            )
        # Deliberately *not* the emptiness check ``consume.drink`` makes: an
        # empty bottle is the normal reason to walk to a sink.
        if view.tainted or view.poisonous:
            raise PreconditionFailed(
                f"{item.display_name} already holds something unsafe to drink",
                reason_code=ReasonCode.NO_SAFE_DRINK,
                evidence={
                    "item_ref": item.ref,
                    "tainted": view.tainted,
                    "poisonous": view.poisonous,
                },
            )
        require_in_main(inventory, item)

        nearby = require_nearby(observation)
        source = nearby_object(nearby, spec.source_ref)
        if source is None:
            raise PreconditionFailed(
                "the observation does not report anything at that square",
                reason_code=ReasonCode.INVALID_REF,
                evidence={"source_ref": spec.source_ref, "observation_seq": observation.seq},
            )
        if self.WATER_SEMANTIC not in source.semantics:
            raise PreconditionFailed(
                f"nothing at {spec.source_ref} reports water",
                reason_code=ReasonCode.NO_SAFE_DRINK,
                evidence={
                    "source_ref": spec.source_ref,
                    "kind": source.kind,
                    "semantics": list(source.semantics),
                },
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _SourceSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _SourceSpec.parse(command)
        start = before.player.thirst
        end = after.player.thirst
        if end >= start:
            return None
        return Evidence(
            kind="thirst_decreased",
            observation_seq=after.seq,
            observed={
                "item_ref": spec.item_ref,
                "fraction": spec.fraction,
                # Carried because the probe's confirmation requires it: without
                # it, an ordinary sip from a bottle would verify a capability
                # §12.4 calls unproven.
                "source_ref": spec.source_ref,
                "thirst_before": start,
                "thirst_after": end,
            },
        )
