"""The probes behind every capability the agent claims (§3.8, §12.5).

A probe has two halves and they are ordered, not interchangeable:

1. **Static resolution** against a :class:`~pz_agent_core.capabilities.scanner.SymbolIndex`
   built from the installed game. Its best possible outcome is
   ``available_unverified``: the symbols are on disk, which is a reason to *try*
   the action, never a reason to claim it works.
2. **Runtime confirmation** from a live game — a succeeded ack for the probe's
   own command, carrying the postcondition evidence the confirmation declares.

The ordering cannot be skipped because :func:`confirm` takes the static
capability as an argument and refuses to upgrade one that was not resolved from
symbols, and because ``verified`` itself requires runtime evidence, which only
:meth:`~pz_agent_core.capabilities.model.Evidence.from_ack` can mint and only
from a succeeded :class:`~pz_agent_core.protocol.ActionResult`.

Every probe also declares a *ceiling*. ``autonomous_attack``'s ceiling is
``unsupported`` with :data:`~pz_agent_core.capabilities.model.REASON_NO_VERIFIED_API`,
so even a live ack cannot raise it: §12.4 lists autonomous combat as unproven,
and the decision not to drive it is a design decision, not a missing probe run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ..protocol import ActionName, ActionResult, CapabilityState
from ..version import TARGET_BUILD
from .model import (
    MAX_REASON_LEN,
    REASON_EXPERIMENTAL_API,
    REASON_NO_VERIFIED_API,
    REASON_PROBE_NOT_CONFIRMED,
    REASON_SYMBOL_MISSING,
    Capability,
    CapabilityError,
    CapabilityReport,
    Evidence,
    EvidenceKind,
    utc_now_iso,
)
from .scanner import SymbolIndex, normalise_symbol

#: The only states a live ack may raise. ``unsupported`` means the symbols are
#: not on this install; ``disabled_by_policy`` means a human turned the
#: capability off, and no amount of evidence overrides that — the API question
#: was never the reason it is off.
_UPGRADEABLE_STATES: Final = frozenset(
    {
        CapabilityState.AVAILABLE_UNVERIFIED,
        CapabilityState.EXPERIMENTAL,
        CapabilityState.VERIFIED,
    }
)

# Capability names, as written in §3.8. They are the keys of the generated
# report and of the MCP tool gate, so they are fixed strings, not derived.
MOVE_TO_SQUARE: Final = "move_to_square"
INVENTORY_TRANSFER: Final = "inventory_transfer"
EAT_PERCENTAGE: Final = "eat_percentage"
DRINK_CARRIED: Final = "drink_carried"
READ_LITERATURE: Final = "read_literature"
DRINK_WORLD_SOURCE: Final = "drink_world_source"
AUTONOMOUS_ATTACK: Final = "autonomous_attack"

# The skills added with the Build 42 adapter set. Each names the Lua class the
# action is actually built from (``docs/GAME_API_VERIFICATION.md``), which is
# what a scan of the user's install can answer for.
#
# Three actions in that set have no entry here on purpose. ``world.inspect``,
# ``container.inspect`` and ``inventory.search`` only *read*, and everything
# they read — squares, containers, items — is reached through Java accessors
# that never appear in the game's Lua. A probe over those names would report
# ``unsupported`` on a perfectly healthy install, so those adapters gate on the
# observation tier they need instead, which is a fact this side can check.
EQUIPMENT_EQUIP: Final = "equipment_equip"
EQUIPMENT_UNEQUIP: Final = "equipment_unequip"
MEDICAL_BANDAGE: Final = "medical_bandage"
SURVIVAL_REST: Final = "survival_rest"
SURVIVAL_SLEEP: Final = "survival_sleep"

# One capability for all three door actions. Opening, closing and unlocking
# all ride the same interaction — a walk into reach, then the game's own door
# toggle — so a build where one works is a build where all three do, and three
# probes over one API would let them disagree about it.
DOOR_TOGGLE: Final = "door_toggle"


@dataclass(frozen=True, slots=True)
class RuntimeConfirmation:
    """How a live confirmation for a probe would arrive.

    ``evidence_keys`` names the postcondition fields the ack must carry. They are
    checked rather than assumed: an ack that says ``succeeded`` while carrying an
    empty or unrelated evidence bag is exactly the fabricated success this
    project treats as a defect.
    """

    action: ActionName
    evidence_keys: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if not self.evidence_keys:
            raise CapabilityError(
                f"{self.action.value}: a confirmation must name the evidence it requires"
            )

    def missing_keys(self, ack: ActionResult) -> tuple[str, ...]:
        return tuple(key for key in self.evidence_keys if key not in ack.evidence)


@dataclass(frozen=True, slots=True)
class ProbeDefinition:
    """One capability, the symbols it needs, and how it could ever be proven."""

    capability: str
    required_symbols: tuple[str, ...]
    confirmation: RuntimeConfirmation
    ceiling: CapabilityState = CapabilityState.VERIFIED
    static_state: CapabilityState = CapabilityState.AVAILABLE_UNVERIFIED
    ceiling_reason: str = ""
    static_reason: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if self.ceiling is not CapabilityState.VERIFIED and not self.ceiling_reason:
            raise CapabilityError(
                f"{self.capability}: a probe that cannot reach 'verified' must say why"
            )
        if self.static_state is CapabilityState.VERIFIED:
            raise CapabilityError(f"{self.capability}: a static scan may never yield 'verified'")

    @property
    def can_be_verified(self) -> bool:
        return self.ceiling is CapabilityState.VERIFIED

    def blocked_capability(self, *, build: str) -> Capability:
        """The capability for a probe whose ceiling forbids ever running it."""
        return Capability.unsupported(
            name=self.capability,
            reason=self.ceiling_reason,
            build=build,
        )


#: Vanilla Build 42 timed actions the blueprint names (§12.2). Requiring the
#: class *and* its constructor keeps a bare forward declaration from counting as
#: a working API.
PROBES: Final[tuple[ProbeDefinition, ...]] = (
    ProbeDefinition(
        capability=MOVE_TO_SQUARE,
        required_symbols=(
            "ISWalkToTimedAction",
            "ISWalkToTimedAction.new",
            "ISTimedActionQueue.add",
        ),
        confirmation=RuntimeConfirmation(
            action=ActionName.MOVEMENT_MOVE_TO,
            evidence_keys=("position",),
            description="the character stands on the requested square after the walk action ends",
        ),
        description="walk to a square using ISWalkToTimedAction",
    ),
    ProbeDefinition(
        capability=INVENTORY_TRANSFER,
        required_symbols=(
            "ISInventoryTransferAction",
            "ISInventoryTransferAction.new",
            "ISTimedActionQueue.add",
        ),
        confirmation=RuntimeConfirmation(
            action=ActionName.INVENTORY_TRANSFER,
            evidence_keys=("item_ref", "container_ref"),
            description="the item is observed inside the destination container",
        ),
        description="move an item between containers using ISInventoryTransferAction",
    ),
    ProbeDefinition(
        capability=EAT_PERCENTAGE,
        required_symbols=("ISEatFoodAction", "ISEatFoodAction.new"),
        confirmation=RuntimeConfirmation(
            action=ActionName.CONSUME_EAT,
            evidence_keys=("hunger_before", "hunger_after"),
            description="hunger fell and the item's remaining fraction dropped as requested",
        ),
        description="eat a chosen fraction of one food item using ISEatFoodAction",
    ),
    ProbeDefinition(
        capability=DRINK_CARRIED,
        required_symbols=("ISDrinkFromBottle", "ISDrinkFromBottle.new"),
        confirmation=RuntimeConfirmation(
            action=ActionName.CONSUME_DRINK,
            evidence_keys=("thirst_before", "thirst_after"),
            description="thirst fell after drinking from a carried container",
        ),
        description="drink from a carried container using ISDrinkFromBottle",
    ),
    ProbeDefinition(
        capability=READ_LITERATURE,
        required_symbols=("ISReadABook", "ISReadABook.new"),
        confirmation=RuntimeConfirmation(
            action=ActionName.LITERATURE_READ,
            evidence_keys=("item_ref", "reading_started"),
            description="the reading action is running and owned by the mod",
        ),
        description="read a book or magazine using ISReadABook",
    ),
    ProbeDefinition(
        capability=EQUIPMENT_EQUIP,
        # The weapon branch only. Wearing a garment goes through ISWearClothing,
        # which the mod resolves at construction time and reports as
        # CAPABILITY_UNAVAILABLE naming the class: a probe is an AND over its
        # symbols, so requiring both here would refuse to draw a weapon on a
        # build that merely spells the clothing action differently.
        required_symbols=(
            "ISEquipWeaponAction",
            "ISEquipWeaponAction.new",
            "ISTimedActionQueue.add",
        ),
        confirmation=RuntimeConfirmation(
            action=ActionName.EQUIPMENT_EQUIP,
            evidence_keys=("item_ref", "slot"),
            description="the item is observed in the hand or body slot it was equipped to",
        ),
        description="put an item in a hand using ISEquipWeaponAction",
    ),
    ProbeDefinition(
        capability=EQUIPMENT_UNEQUIP,
        required_symbols=("ISUnequipAction", "ISUnequipAction.new", "ISTimedActionQueue.add"),
        confirmation=RuntimeConfirmation(
            action=ActionName.EQUIPMENT_UNEQUIP,
            evidence_keys=("item_ref", "container_ref"),
            description="the item is in no slot and still in a container on the character",
        ),
        description="take an item off using ISUnequipAction",
    ),
    ProbeDefinition(
        capability=MEDICAL_BANDAGE,
        required_symbols=("ISApplyBandage", "ISApplyBandage.new", "ISTimedActionQueue.add"),
        confirmation=RuntimeConfirmation(
            action=ActionName.MEDICAL_BANDAGE,
            evidence_keys=("body_part", "bleeding_after"),
            description="the dressed body part is no longer reported bleeding",
        ),
        description="dress a bleeding body part using ISApplyBandage",
    ),
    ProbeDefinition(
        capability=SURVIVAL_REST,
        # Resting is mostly the *absence* of a queued action, and the two
        # sitting classes differ between builds — a build with only one must
        # degrade to that one rather than refuse to rest — so the queue is the
        # single symbol every branch of this action needs. Which posture was
        # actually available is reported by the mod, per attempt.
        required_symbols=("ISTimedActionQueue.add",),
        confirmation=RuntimeConfirmation(
            action=ActionName.SURVIVAL_REST,
            evidence_keys=("endurance_before", "endurance_after"),
            description="endurance rose to the target that was asked for",
        ),
        description="recover endurance by standing or sitting still",
    ),
    ProbeDefinition(
        capability=SURVIVAL_SLEEP,
        # Sleeping has no timed-action class: vanilla drives it from a context
        # menu callback, which is the least certain entry in
        # docs/GAME_API_VERIFICATION.md and the most consequential one, since a
        # sleeping character cannot be woken. 'experimental' is therefore the
        # best a healthy scan may report — the symbol being present says nothing
        # about whether driving it is safe for the save.
        required_symbols=("ISWorldObjectContextMenu", "ISWorldObjectContextMenu.onSleep"),
        confirmation=RuntimeConfirmation(
            action=ActionName.SURVIVAL_SLEEP,
            evidence_keys=("fatigue_before", "fatigue_after", "elapsed_game_seconds"),
            description="fatigue fell and the world clock advanced across the night",
        ),
        static_state=CapabilityState.EXPERIMENTAL,
        static_reason=REASON_EXPERIMENTAL_API,
        description="sleep in a bed using the vanilla context-menu entry point",
    ),
    ProbeDefinition(
        capability=DRINK_WORLD_SOURCE,
        # §12.4 lists the world water source action as unconfirmed, so the best
        # a healthy scan may report is 'experimental': the symbol's presence
        # says nothing about whether driving it is safe for the save.
        required_symbols=("ISTakeWaterAction", "ISTakeWaterAction.new"),
        confirmation=RuntimeConfirmation(
            # Its own action, not ``consume.drink``. ``source_ref`` is what
            # separates drinking from a sink from drinking from a bottle, and
            # while both shared one action a sip could have carried it.
            action=ActionName.CONSUME_DRINK_SOURCE,
            evidence_keys=("thirst_before", "thirst_after", "source_ref"),
            description="thirst fell after drinking from a world water source",
        ),
        static_state=CapabilityState.EXPERIMENTAL,
        static_reason=REASON_EXPERIMENTAL_API,
        description="drink from a sink, well or rain collector",
    ),
    ProbeDefinition(
        capability=DOOR_TOGGLE,
        # The walk into reach is the only half of the interaction the game's
        # Lua ever names. The toggle itself is ``IsoDoor.ToggleDoor``, a Java
        # method no Lua scan can see, so requiring it here would report
        # ``unsupported`` on a perfectly healthy install; the mod probes the
        # resolved door for the method per command and refuses naming the
        # symbol when a build lacks it.
        required_symbols=(
            "ISWalkToTimedAction",
            "ISWalkToTimedAction.new",
            "ISTimedActionQueue.add",
        ),
        confirmation=RuntimeConfirmation(
            action=ActionName.DOOR_OPEN,
            evidence_keys=("door_ref", "open_before", "open_after"),
            description="the door reads open when re-read after the toggle",
        ),
        description="open, close and unlock doors through the game's own door interaction",
    ),
    ProbeDefinition(
        capability=AUTONOMOUS_ATTACK,
        required_symbols=(),
        confirmation=RuntimeConfirmation(
            action=ActionName.ACTION_WAIT,
            evidence_keys=("never",),
            description="no confirmation is accepted; the capability is refused by design",
        ),
        ceiling=CapabilityState.UNSUPPORTED,
        ceiling_reason=REASON_NO_VERIFIED_API,
        static_state=CapabilityState.UNSUPPORTED,
        static_reason=REASON_NO_VERIFIED_API,
        description="autonomous combat — no API this project is willing to drive (§12.4)",
    ),
)

PROBES_BY_NAME: Final[dict[str, ProbeDefinition]] = {p.capability: p for p in PROBES}


def probe_for(capability: str) -> ProbeDefinition:
    """Look up a probe by capability name.

    Raises:
        CapabilityError: for an unknown name. Returning a permissive default here
            would let a typo in a tool definition open an ungated write path.
    """
    probe = PROBES_BY_NAME.get(capability)
    if probe is None:
        raise CapabilityError(f"no probe declared for capability {capability!r}")
    return probe


def resolve_static(
    probe: ProbeDefinition,
    index: SymbolIndex,
    *,
    build: str,
    observed_at: str | None = None,
) -> Capability:
    """Resolve *probe* against a symbol index.

    The result is never ``verified``: this function has no access to a running
    game, and it only ever mints ``static_scan`` evidence.
    """
    when = observed_at or utc_now_iso()
    if not probe.can_be_verified:
        return probe.blocked_capability(build=build)

    missing = index.missing(probe.required_symbols)
    if missing:
        reason = f"{REASON_SYMBOL_MISSING}: {', '.join(missing[:4])}"
        return Capability.unsupported(
            name=probe.capability,
            reason=reason[:MAX_REASON_LEN],
            build=build,
        )

    evidence: list[Evidence] = []
    for symbol in probe.required_symbols:
        record = index.find(symbol)
        if record is None:  # pragma: no cover - `missing` above already excluded this
            continue
        evidence.append(
            Evidence.from_scan(
                symbol=record.symbol,
                file=record.relative_path,
                file_sha256=record.file_sha256,
                signature=record.signature,
                observed_at=when,
            )
        )

    if probe.static_state is CapabilityState.EXPERIMENTAL:
        return Capability.experimental(
            name=probe.capability,
            build=build,
            reason=probe.static_reason or REASON_EXPERIMENTAL_API,
            evidence=evidence,
        )
    return Capability.available_unverified(
        name=probe.capability,
        build=build,
        evidence=evidence,
        reason=probe.static_reason,
    )


def confirm(
    probe: ProbeDefinition,
    static: Capability,
    ack: ActionResult,
    *,
    build: str,
    observed_at: str | None = None,
) -> Capability:
    """Upgrade a statically resolved capability with a live ack.

    *static* must be the capability :func:`resolve_static` produced for the same
    probe. That argument is what makes the ordering structural: there is no way
    to reach ``verified`` without first having found the symbols on the machine
    the game is installed on.

    A non-succeeded ack, an ack for the wrong action, or an ack missing the
    declared postcondition keys all record the refusal instead, and a capability
    an earlier run had marked ``verified`` falls back to ``available_unverified``
    rather than keeping a claim this run did not support. Nothing here can
    fabricate a success.
    """
    when = observed_at or utc_now_iso()
    if static.name != probe.capability:
        raise CapabilityError(
            f"capability {static.name!r} cannot be confirmed by probe {probe.capability!r}"
        )
    if not probe.can_be_verified:
        return probe.blocked_capability(build=build)
    if static.state not in _UPGRADEABLE_STATES:
        # ``unsupported``: the symbols are not on this install, so a succeeded ack
        # for something else must not resurrect the claim. ``disabled_by_policy``:
        # a human turned it off, and a probe result is not a permission.
        return static
    unproven = _unproven_symbols(probe, static)
    if unproven:
        # The caller handed in something that was not produced by resolve_static
        # against this machine. Without a static finding for every required
        # symbol there is no chain from "the API exists here" to "it worked".
        return _not_confirmed(static, f"no static finding for {', '.join(unproven[:4])}")
    if ack.action != probe.confirmation.action.value:
        return _not_confirmed(static, f"ack for {ack.action}, expected {probe.confirmation.action}")

    missing = probe.confirmation.missing_keys(ack)
    if missing:
        return _not_confirmed(static, f"ack lacks evidence {', '.join(missing)}")

    try:
        runtime = Evidence.from_ack(
            probe=probe.capability,
            symbol=probe.required_symbols[0] if probe.required_symbols else probe.capability,
            ack=ack,
            observed_at=when,
        )
    except CapabilityError as exc:
        return _not_confirmed(static, str(exc))

    return Capability.verified(
        name=probe.capability,
        build=build,
        evidence=(*static.evidence, runtime),
    )


def _unproven_symbols(probe: ProbeDefinition, static: Capability) -> tuple[str, ...]:
    """Required symbols the *static* capability carries no scan evidence for."""
    scanned = {
        normalise_symbol(e.symbol) for e in static.evidence if e.kind is EvidenceKind.STATIC_SCAN
    }
    return tuple(s for s in probe.required_symbols if normalise_symbol(s) not in scanned)


def _not_confirmed(static: Capability, detail: str) -> Capability:
    """The capability as it stands after a confirmation attempt that failed.

    A capability that was ``verified`` by an earlier run drops back to
    ``available_unverified``. §3.8 says the report holds probe *results*, not
    promises: a run that did not confirm cannot leave "verified" on the record,
    or the MCP would publish a ready write tool on the strength of a probe that
    just failed. The earlier evidence is kept — it happened — but it no longer
    carries a verified claim on its own.
    """
    reason = f"{REASON_PROBE_NOT_CONFIRMED}: {detail}"
    state = (
        CapabilityState.AVAILABLE_UNVERIFIED
        if static.state is CapabilityState.VERIFIED
        else static.state
    )
    return Capability(
        name=static.name,
        state=state,
        reason=reason[:MAX_REASON_LEN],
        build=static.build,
        evidence=static.evidence,
    )


def resolve_all(
    index: SymbolIndex,
    *,
    build: str = TARGET_BUILD,
    probes: Sequence[ProbeDefinition] = PROBES,
    observed_at: str | None = None,
    revision: int = 1,
) -> CapabilityReport:
    """Static pass over every declared probe, as one report.

    This is what ``pz-agent doctor`` produces before any game session exists. No
    capability in it is ``verified``, by construction.
    """
    when = observed_at or utc_now_iso()
    capabilities = [resolve_static(probe, index, build=build, observed_at=when) for probe in probes]
    return CapabilityReport(
        build=build,
        capabilities=tuple(sorted(capabilities, key=lambda c: c.name)),
        revision=revision,
        generated_at=when,
    )


def missing_symbols(
    index: SymbolIndex, probes: Iterable[ProbeDefinition] = PROBES
) -> dict[str, tuple[str, ...]]:
    """Capability → the symbols the local install does not declare.

    ``doctor`` prints this so the user sees exactly which API is absent rather
    than a bare "unsupported".
    """
    result: dict[str, tuple[str, ...]] = {}
    for probe in probes:
        gaps = index.missing(probe.required_symbols)
        if gaps:
            result[probe.capability] = gaps
    return result
