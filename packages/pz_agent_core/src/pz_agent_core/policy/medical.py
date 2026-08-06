"""Deterministic triage: which wound to dress, and with what.

Which wound the character treats first is decided here, by code, and never by a
language model — for the same reason the sandwich is (§5.14): a model that picks
the wound is a model that eventually picks the dirty rag while a clean bandage
is in the bag. The model may say "I am hurt"; :func:`select_treatment` says
*which part*, *which dressing*, and — when it refuses — exactly what was wrong
with every dressing it looked at.

The ordering is not a preference, it is a claim about the game, and each step of
it is defensible on its own:

1. **Bleeding outranks not bleeding.** Blood loss is the only injury here that
   kills on its own and on a clock; a fracture hurts and waits.
2. **Deeper outranks shallow.** Among bleeding wounds the deep one loses blood
   faster, so severity breaks the tie before anything cosmetic does.
3. **The part's own name breaks the last tie.** Two equally hurt forearms have
   to be treated in a fixed order, or a retry after an interrupted attempt would
   pick the other one and neither would ever get finished.
4. **A sterile dressing beats a dirty one, and a dirty one is refused outright
   while a clean one is in reach.** An infected bandage is worse than none: it
   trades bleeding — which stops — for an infection, which does not.

Everything here is a pure function of the observation. No clock, no random
source, and every ranking ends in a total order, so two calls over an unchanged
world answer the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..protocol import InventoryView, ItemView, JsonDict, PlayerState, ReasonCode, Wound
from .config import DEFAULT_POLICY_CONFIG, PolicyConfig
from .selection import (
    Rejection,
    RejectionReason,
    bounded_rejections,
    is_user_reserved,
    needs_transfer_to_main,
    reach_rejection,
    truncation_note,
)

__all__ = [
    "DIRTY_DRESSING_TYPES",
    "DRESSING_TAG",
    "STERILE_DRESSING_TYPES",
    "Dressing",
    "TreatmentChoice",
    "TreatmentSelection",
    "select_treatment",
    "wound_body_part",
]

#: Dressings this policy will choose on its own, best first. Closed and ordered
#: on purpose: a mod-added dressing is a decision the *user* makes by naming the
#: item, not one made here by a name that happens to contain "bandage".
STERILE_DRESSING_TYPES: Final[tuple[str, ...]] = (
    "Base.Bandage",
    "Base.RippedSheets",
    "Base.DenimStrips",
    "Base.LeatherStrips",
)

#: The same materials once they have been used or dropped in the dirt. Usable,
#: and only when nothing clean is left.
DIRTY_DRESSING_TYPES: Final[tuple[str, ...]] = (
    "Base.RippedSheetsDirty",
    "Base.DenimStripsDirty",
)

#: Tag the mod may attach to anything it considers a dressing. Honoured so a
#: build that spells a type differently is still treatable, but only as a
#: *fallback* rank: a tagged unknown never outranks a named sterile bandage.
DRESSING_TAG: Final = "bandage"

#: Ranks, low is better. They exist as numbers rather than as list positions so
#: a tagged-but-unknown dressing can sit between "clean" and "dirty" without
#: being inserted into either closed list.
_RANK_STERILE: Final = 0
_RANK_TAGGED: Final = 1
_RANK_DIRTY: Final = 2


def wound_body_part(wound: Wound) -> str:
    """The body location a wound reference names.

    Wound references are ``wound:<session>:<BodyPart>`` — the part is the tail,
    and it is the only place the observation says *where* the injury is. A
    reference without one yields an empty string, which no command can name.
    """
    parts = wound.ref.split(":")
    return parts[2] if len(parts) == 3 else ""


@dataclass(frozen=True, slots=True)
class Dressing:
    """One candidate dressing, classified.

    ``sterile`` is a property of the item type rather than of the situation, so
    it is decided once here and the refusal rule ("no dirty dressing while a
    clean one exists") is applied against the whole candidate set rather than
    item by item.
    """

    item: ItemView
    rank: int

    @property
    def sterile(self) -> bool:
        return self.rank == _RANK_STERILE

    @property
    def dirty(self) -> bool:
        return self.rank == _RANK_DIRTY

    @classmethod
    def classify(cls, item: ItemView) -> Dressing | None:
        """Rank *item* as a dressing, or None when it is not one at all."""
        if item.full_type in STERILE_DRESSING_TYPES:
            return cls(item=item, rank=_RANK_STERILE)
        if item.full_type in DIRTY_DRESSING_TYPES:
            return cls(item=item, rank=_RANK_DIRTY)
        if DRESSING_TAG in {tag.lower() for tag in item.tags}:
            return cls(item=item, rank=_RANK_TAGGED)
        return None

    @property
    def order_key(self) -> tuple[int, int, str]:
        """Total order: cleanliness, then the closed list, then the reference.

        The item reference is the last key and is unique within a session, so no
        two candidates compare equal and the choice cannot depend on the order
        the mod happened to enumerate the inventory in.
        """
        try:
            position = STERILE_DRESSING_TYPES.index(self.item.full_type)
        except ValueError:
            position = len(STERILE_DRESSING_TYPES)
        return self.rank, position, self.item.ref


@dataclass(frozen=True, slots=True)
class TreatmentChoice:
    """The wound to dress, the dressing to use, and why."""

    body_part: str
    wound_ref: str
    wound_kind: str
    severity: float
    bleeding: bool
    item_ref: str
    display_name: str
    sterile: bool
    needs_transfer_to_main: bool
    rationale: str

    def as_dict(self) -> JsonDict:
        return {
            "body_part": self.body_part,
            "wound_ref": self.wound_ref,
            "wound_kind": self.wound_kind,
            "severity": self.severity,
            "bleeding": self.bleeding,
            "item_ref": self.item_ref,
            "display_name": self.display_name,
            "sterile": self.sterile,
            "needs_transfer_to_main": self.needs_transfer_to_main,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class TreatmentSelection:
    """A choice or a refusal, plus every dressing that was turned down.

    The rejections travel with both outcomes for the same reason they do in
    :mod:`~pz_agent_core.policy.food`: on a refusal they *are* the answer, and
    on a success they are what lets the agent say "the only clean bandage is in
    the crate, so I used the ripped sheet" instead of an unexplained pick.
    """

    choice: TreatmentChoice | None
    reason_code: ReasonCode | None
    rejections: tuple[Rejection, ...]
    rejected_count: int = 0
    #: Empty when a wound was found; otherwise why nothing needed treating.
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.choice is None) == (self.reason_code is None):
            raise ValueError("a selection is either a choice or a refusal, never both or neither")
        if self.rejected_count < len(self.rejections):
            raise ValueError(
                f"rejected_count {self.rejected_count} is below the "
                f"{len(self.rejections)} rejections carried"
            )

    @property
    def is_refusal(self) -> bool:
        return self.choice is None

    @property
    def rejections_truncated(self) -> bool:
        return self.rejected_count > len(self.rejections)

    @classmethod
    def chosen(
        cls,
        choice: TreatmentChoice,
        *,
        rejections: tuple[Rejection, ...],
        rejected_count: int,
    ) -> TreatmentSelection:
        return cls(
            choice=choice,
            reason_code=None,
            rejections=rejections,
            rejected_count=rejected_count,
        )

    @classmethod
    def refused(
        cls,
        *,
        reason_code: ReasonCode,
        detail: str,
        rejections: tuple[Rejection, ...] = (),
        rejected_count: int = 0,
    ) -> TreatmentSelection:
        return cls(
            choice=None,
            reason_code=reason_code,
            rejections=rejections,
            rejected_count=rejected_count,
            detail=detail,
        )

    def explain(self) -> str:
        """A sentence for the user, whichever way the selection went."""
        if self.choice is not None:
            return self.choice.rationale
        if not self.rejections:
            return self.detail
        reasons = "; ".join(r.describe() for r in self.rejections)
        note = truncation_note(len(self.rejections), self.rejected_count)
        return f"{self.detail}: {reasons}{note}"

    def as_dict(self) -> JsonDict:
        return {
            "choice": None if self.choice is None else self.choice.as_dict(),
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "detail": self.detail,
            "rejections": [r.as_dict() for r in self.rejections],
            "rejected_count": self.rejected_count,
        }


def _wound_order_key(wound: Wound) -> tuple[int, float, str]:
    """Triage order for one wound, best-first once negated where it matters.

    Sorting on ``(not bleeding, -severity, part)`` puts every bleeding wound
    ahead of every non-bleeding one whatever their severities — which is the
    point of rule 1: a scratch that is bleeding outranks a deep bruise that is
    not, because only one of the two is still losing blood.
    """
    return (0 if wound.bleeding else 1, -wound.severity, wound_body_part(wound))


def _treatable_wounds(player: PlayerState) -> tuple[Wound, ...]:
    """Bleeding wounds, worst first.

    Only bleeding ones: a dressing's effect on anything else is not something
    the observation reports, so a command to treat a non-bleeding wound could
    never be shown to have worked. Refusing here is what keeps the adapter from
    being asked for a success it cannot observe.
    """
    return tuple(
        sorted(
            (wound for wound in player.wounds if wound.bleeding and wound_body_part(wound)),
            key=_wound_order_key,
        )
    )


def _reject(item: ItemView, reason: RejectionReason, detail: str) -> Rejection:
    return Rejection(
        item_ref=item.ref,
        display_name=item.display_name,
        reason=reason,
        detail=detail,
    )


def select_treatment(
    inventory: InventoryView,
    player: PlayerState,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> TreatmentSelection:
    """Pick the wound to dress and the dressing to dress it with.

    Args:
        inventory: the containers and items the mod enumerated. A dressing whose
            container is missing from the view is refused rather than guessed
            at.
        player: read for wounds only; nothing here mutates it.
        config: reach and reserve rules, shared with the other policies.

    Returns:
        A :class:`TreatmentSelection` carrying either a choice or a refusal
        with :attr:`~pz_agent_core.protocol.ReasonCode.PRECONDITION_FAILED`
        (nothing is bleeding) or
        :attr:`~pz_agent_core.protocol.ReasonCode.RESOURCE_RESERVED` (there are
        dressings, and every one of them was refused), together with one
        rejection per dressing examined — trimmed to
        ``config.max_reported_rejections``, with the untrimmed total in
        ``rejected_count``.
    """
    wounds = _treatable_wounds(player)
    if not wounds:
        return TreatmentSelection.refused(
            reason_code=ReasonCode.PRECONDITION_FAILED,
            detail="nothing on this character is bleeding",
        )
    wound = wounds[0]

    rejections: list[Rejection] = []
    candidates: list[Dressing] = []
    for item in inventory.items:
        dressing = Dressing.classify(item)
        if dressing is None:
            continue  # not a dressing at all: not a rejected candidate, just noise
        rejection = _first_rejection(dressing, inventory, config)
        if rejection is not None:
            rejections.append(rejection)
            continue
        candidates.append(dressing)

    # The dirty-dressing rule is applied here rather than in the filters because
    # it is the only one that depends on the *set*: a dirty rag is refusable
    # only while something clean survived every other filter.
    clean = [c for c in candidates if not c.dirty]
    if clean:
        for dirty in (c for c in candidates if c.dirty):
            rejections.append(
                _reject(
                    dirty.item,
                    RejectionReason.TAINTED,
                    "a clean dressing is in reach, and a dirty one trades bleeding for "
                    "an infection that does not stop on its own",
                )
            )
        candidates = clean

    reported = bounded_rejections(rejections, config)
    if not candidates:
        return TreatmentSelection.refused(
            reason_code=ReasonCode.RESOURCE_RESERVED,
            detail=f"nothing in reach can dress the bleeding {wound_body_part(wound)}",
            rejections=reported,
            rejected_count=len(rejections),
        )

    best = min(candidates, key=lambda c: c.order_key)
    transfer = needs_transfer_to_main(inventory, best.item)
    return TreatmentSelection.chosen(
        TreatmentChoice(
            body_part=wound_body_part(wound),
            wound_ref=wound.ref,
            wound_kind=wound.kind,
            severity=wound.severity,
            bleeding=wound.bleeding,
            item_ref=best.item.ref,
            display_name=best.item.display_name,
            sterile=best.sterile,
            needs_transfer_to_main=transfer,
            rationale=_rationale(wound, best, wounds=wounds, transfer=transfer),
        ),
        rejections=reported,
        rejected_count=len(rejections),
    )


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------
# Order is fixed and meaningful: the reported reason is the first one that
# matches, and the list runs from "this is not yours" through "this is not
# reachable" to "the game says it is gone". A user reading a refusal gets the
# most fundamental problem with the item rather than whichever check ran first.


def _first_rejection(
    dressing: Dressing, inventory: InventoryView, config: PolicyConfig
) -> Rejection | None:
    item = dressing.item
    if is_user_reserved(item, config):
        return _reject(
            item,
            RejectionReason.USER_RESERVED,
            "you marked it as reserved, so it is never taken automatically",
        )
    reach = reach_rejection(inventory, item, config)
    if reach is not None:
        return reach
    if item.equipped:
        return _reject(
            item,
            RejectionReason.NOT_REQUESTED_ITEM,
            "it is equipped, and taking it off is a separate action",
        )
    return None


def _rationale(
    wound: Wound,
    dressing: Dressing,
    *,
    wounds: tuple[Wound, ...],
    transfer: bool,
) -> str:
    part = wound_body_part(wound)
    parts = [
        f"{part} is bleeding at severity {wound.severity:.2f} ({wound.kind})",
        f"dressing it with {dressing.item.display_name}"
        + ("" if dressing.sterile else " — nothing sterile is in reach"),
    ]
    if len(wounds) > 1:
        others = ", ".join(wound_body_part(w) for w in wounds[1:4])
        parts.append(f"{len(wounds) - 1} other bleeding wound(s) wait their turn: {others}")
    if transfer:
        parts.append("it has to be moved to the main inventory first")
    return "; ".join(parts)
