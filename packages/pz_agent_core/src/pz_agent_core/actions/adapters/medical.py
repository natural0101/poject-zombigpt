"""``medical.bandage``: dressing one wound, and proving it.

The postcondition is read off the body part the command named: the wound the
character was losing blood from is no longer reported as bleeding. Nothing here
writes a flag on the damage model, and nothing here treats "the dressing left
the inventory" as proof — a bandage that was used up and a bandage that was
dropped look identical from the item list, and only one of them stopped the
bleeding.

Two things this adapter deliberately does not do.

**It does not choose.** Which part and which dressing are
:func:`pz_agent_core.policy.medical.select_treatment`'s decision, made
deterministically and separately tested, and both arrive in the command.
Duplicating the triage here would create a second copy of the rule that refuses
a dirty dressing while a clean one is in reach — and a second copy is a copy no
test compares against the first.

**It does not treat what it cannot observe.** The observation reports a wound's
``bleeding`` flag; it reports no bandage flag at all. Dressing a wound that is
not bleeding is therefore refused rather than attempted, because the only thing
that could follow is an ack this side would have to take on trust. That is a
narrower action than the mod's — ``adapters/Medical.lua`` is willing to bandage
anything the game calls a wound — and the narrowing is on purpose: this side
only sends what it can check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import MEDICAL_BANDAGE
from ...policy.medical import Dressing, wound_body_part
from ...protocol import (
    ActionName,
    Command,
    JsonDict,
    Observation,
    PlayerState,
    ReasonCode,
    RefKind,
    RiskClass,
    Wound,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    check_args,
    find_by_identity,
    identity_of,
    read_ref,
    require_inventory,
    resolve_item,
)
from .consume import require_in_main

__all__ = [
    "BODY_PARTS",
    "DEFAULT_BANDAGE_POLL_MS",
    "DEFAULT_BANDAGE_TIMEOUT_MS",
    "BandageAdapter",
]

#: Body parts a command may name, spelled the way ``BodyPartType`` spells them
#: and the way the mod's wound references carry them.
#:
#: Closed for two reasons. A build that grows a part treats it only once it is
#: listed here, which is the direction to be wrong in; and ``body_part`` is used
#: to look a wound up by name, so an unchecked value would be a lookup on a
#: string that arrived from a model.
BODY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "Head",
        "Neck",
        "Torso_Upper",
        "Torso_Lower",
        "Groin",
        "UpperArm_L",
        "UpperArm_R",
        "ForeArm_L",
        "ForeArm_R",
        "Hand_L",
        "Hand_R",
        "UpperLeg_L",
        "UpperLeg_R",
        "LowerLeg_L",
        "LowerLeg_R",
        "Foot_L",
        "Foot_R",
    }
)

DEFAULT_BANDAGE_TIMEOUT_MS: Final = 30_000
DEFAULT_BANDAGE_POLL_MS: Final = 250


def _wound_on(player: PlayerState, body_part: str) -> Wound | None:
    """The wound reported for one body part, or None when none is.

    A part with no wound in the list is not injured — which after a successful
    dressing is exactly what the observation should say, and is why the absence
    is treated as an outcome here rather than as missing data.
    """
    return next((w for w in player.wounds if wound_body_part(w) == body_part), None)


@dataclass(frozen=True, slots=True)
class _BandageSpec:
    """The part to dress and the dressing to dress it with.

    Both are required, unlike on the mod side where either may be omitted. The
    reason is the one in the module docstring: with the choice made by the
    policy and carried in the command, ``verify`` knows which part to look at
    even after the wound it was chosen for has stopped bleeding — re-deriving
    "the worst bleeding part" from the world as it is *now* would go looking at
    a different part precisely when the action worked.
    """

    body_part: str
    item_ref: str

    @classmethod
    def parse(cls, command: Command) -> _BandageSpec:
        check_args(command, allowed=("body_part", "item_ref"), required=("body_part", "item_ref"))
        raw = command.args.get("body_part")
        if not isinstance(raw, str) or raw not in BODY_PARTS:
            raise PreconditionFailed(
                f"body_part must be one of the {len(BODY_PARTS)} parts this character has",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"body_part": raw if isinstance(raw, str) else None},
            )
        return cls(body_part=raw, item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM))

    def as_args(self) -> JsonDict:
        return {"body_part": self.body_part, "item_ref": self.item_ref}


@dataclass(frozen=True, slots=True)
class BandageAdapter:
    """``medical.bandage``: dress one bleeding wound with one carried dressing."""

    timeout_ms: int = DEFAULT_BANDAGE_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_BANDAGE_POLL_MS

    name: ClassVar[ActionName] = ActionName.MEDICAL_BANDAGE
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = MEDICAL_BANDAGE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _BandageSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        if Dressing.classify(item) is None:
            raise PreconditionFailed(
                f"{item.display_name} is not a dressing",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "full_type": item.full_type},
            )
        # Deliberately not the triage filters: whether a dirty dressing may be
        # used, and whether a reserved one may be spent, are policy.medical's
        # decisions and are already made by the time a reference reaches here.
        wound = _wound_on(observation.player, spec.body_part)
        if wound is None:
            raise PreconditionFailed(
                f"no wound is reported on {spec.body_part}",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"body_part": spec.body_part},
            )
        if not wound.bleeding:
            raise PreconditionFailed(
                f"{spec.body_part} is not bleeding, and the observation reports no "
                "dressing state, so applying one could not be verified",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={
                    "body_part": spec.body_part,
                    "wound_ref": wound.ref,
                    "wound_kind": wound.kind,
                },
            )
        require_in_main(inventory, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _BandageSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _BandageSpec.parse(command)
        was = _wound_on(before.player, spec.body_part)
        if was is None or not was.bleeding:
            # Nothing was bleeding there to begin with, so nothing about the
            # part afterwards can prove this command did anything.
            return None
        now = _wound_on(after.player, spec.body_part)
        if now is not None and now.bleeding:
            return None
        identity = identity_of(spec.item_ref)
        used = after.inventory is not None and not find_by_identity(after.inventory, identity)
        return Evidence(
            kind="bleeding_stopped",
            observation_seq=after.seq,
            observed={
                "body_part": spec.body_part,
                "wound_ref": was.ref,
                "wound_kind": was.kind,
                "bleeding_before": True,
                "bleeding_after": False if now is None else now.bleeding,
                "severity_before": was.severity,
                "severity_after": None if now is None else now.severity,
                "wound_still_reported": now is not None,
                "item_ref": spec.item_ref,
                # Context, never proof: a dressing can leave the inventory by
                # being dropped, so this says what happened to it and claims
                # nothing about why the bleeding stopped.
                "dressing_consumed": used,
            },
        )
