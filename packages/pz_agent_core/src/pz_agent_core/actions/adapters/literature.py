"""``literature.read``: read a bounded number of pages, and prove they moved.

§4.10 is explicit that "started" is a different result from "read": a tool that
returns success the moment ``ISReadABook`` is queued has reported the queue, not
the reading. So this adapter takes a page target and its postcondition is a page
counter that actually advanced — the one signal that cannot be produced by
anything except the character reading.

The target is bounded and clamped to what is left in the item. Reading a whole
skill book under one command id would hold a lease open across an hour of world
time in which the reasons for reading almost certainly changed; the caller reads
a chapter, re-observes, and decides again.

Two preconditions are refusals rather than scores, because no amount of wanting
overcomes them: the Illiterate trait, and an item with nothing left unread.
Everything else about *which* book — skill window, unread recipes, goal fit — is
:mod:`pz_agent_core.policy.literature`'s decision and is not repeated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities import READ_LITERATURE
from ...policy.literature import LiteratureView, is_illiterate
from ...protocol import (
    ActionName,
    ActionOwnership,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    ItemIdentity,
    check_args,
    find_by_identity,
    identity_of,
    read_count,
    read_ref,
    require_inventory,
    resolve_item,
)
from .consume import require_in_main

__all__ = [
    "DEFAULT_READ_PAGES",
    "DEFAULT_READ_POLL_MS",
    "DEFAULT_READ_TIMEOUT_MS",
    "MAX_READ_PAGES",
    "READING_ACTION_MARKER",
    "READ_NO_PROGRESS_MS",
    "ReadAdapter",
]

#: Pages in one command. The ceiling keeps a single lease from covering a whole
#: book; the default is roughly a chapter.
MAX_READ_PAGES: Final = 200
DEFAULT_READ_PAGES: Final = 20

DEFAULT_READ_TIMEOUT_MS: Final = 120_000
DEFAULT_READ_POLL_MS: Final = 500

#: Reading advances a page counter far more slowly than a walk closes distance.
#: An engine driving this action needs its no-progress window widened to at
#: least this, or a perfectly healthy read is cancelled between two pages.
READ_NO_PROGRESS_MS: Final = 20_000

#: How a reading action identifies itself in ``action.type``. Recorded in the
#: evidence as the "reading started" half of §4.10's postcondition.
READING_ACTION_MARKER: Final = "read"


@dataclass(frozen=True, slots=True)
class _ReadSpec:
    item_ref: str
    pages: int

    @classmethod
    def parse(cls, command: Command) -> _ReadSpec:
        check_args(command, allowed=("item_ref", "pages"), required=("item_ref",))
        return cls(
            item_ref=read_ref(command, "item_ref", kind=RefKind.ITEM),
            pages=read_count(
                command, "pages", default=DEFAULT_READ_PAGES, minimum=1, maximum=MAX_READ_PAGES
            ),
        )

    def as_args(self) -> JsonDict:
        return {"item_ref": self.item_ref, "pages": self.pages}


def _view(observation: Observation, identity: ItemIdentity) -> LiteratureView | None:
    """The literature sub-object of the tracked item in *observation*."""
    if observation.inventory is None:
        return None
    found = find_by_identity(observation.inventory, identity)
    if not found:
        return None
    return LiteratureView.from_item(found[0])


def _is_reading(observation: Observation) -> bool:
    """True when the mod's own queue entry looks like a reading action."""
    state = observation.action
    if state.ownership is not ActionOwnership.MOD or not state.busy:
        return False
    return READING_ACTION_MARKER in (state.type or "").lower()


@dataclass(frozen=True, slots=True)
class ReadAdapter:
    """``literature.read``: advance a book by a bounded number of pages.

    Risk P2 rather than P1: reading is reversible and consumes nothing, but it
    immobilises the character for a long stretch of world time, which is the
    property the permission tiers are actually about.
    """

    timeout_ms: int = DEFAULT_READ_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_READ_POLL_MS

    name: ClassVar[ActionName] = ActionName.LITERATURE_READ
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = READ_LITERATURE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _ReadSpec.parse(command)
        inventory = require_inventory(observation)
        item = resolve_item(inventory, spec.item_ref)
        view = LiteratureView.from_item(item)
        if view is None:
            raise PreconditionFailed(
                f"{item.display_name} is not something that can be read",
                reason_code=ReasonCode.INVALID_ARGUMENT,
                evidence={"item_ref": item.ref, "category": item.category},
            )
        if is_illiterate(observation.player):
            raise PreconditionFailed(
                "the character is Illiterate and cannot read at all",
                reason_code=ReasonCode.POLICY_DENIED,
                evidence={"item_ref": item.ref},
            )
        if view.destroyed:
            raise PreconditionFailed(
                f"{item.display_name} is destroyed",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"item_ref": item.ref},
            )
        if view.is_finished:
            raise PreconditionFailed(
                f"{item.display_name} has already been read through",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={
                    "item_ref": item.ref,
                    "pages_read": view.pages_read,
                    "pages_total": view.pages_total,
                },
            )
        if view.pages_total <= 0:
            # Without a page count there is no counter to watch, so the action
            # could be queued but never verified.
            raise PreconditionFailed(
                f"the mod reported no page count for {item.display_name}, "
                "so reading progress cannot be verified",
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                evidence={"item_ref": item.ref},
            )
        require_in_main(inventory, item)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        spec = _ReadSpec.parse(command)
        item = resolve_item(require_inventory(observation), spec.item_ref)
        view = LiteratureView.from_item(item)
        remaining = spec.pages if view is None else min(spec.pages, view.pages_remaining)
        return _ReadSpec(item_ref=spec.item_ref, pages=max(1, remaining)).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _ReadSpec.parse(command)
        identity = identity_of(spec.item_ref)
        started = _view(before, identity)
        current = _view(after, identity)
        if started is None or current is None:
            return None
        advanced = current.pages_read - started.pages_read
        if advanced <= 0:
            return None
        if advanced < spec.pages and not current.is_finished:
            return None
        return Evidence(
            kind="reading_progress_advanced",
            observation_seq=after.seq,
            observed={
                "item_ref": spec.item_ref,
                # A page counter cannot move without the read action having
                # run, so the advance is the stronger half of §4.10's "started
                # and progressed" pair; the queue state is recorded beside it
                # rather than relied on, because a read that finished within
                # one poll leaves the queue idle again.
                "reading_started": True,
                "reading_action_observed": _is_reading(after),
                "pages_before": started.pages_read,
                "pages_after": current.pages_read,
                "pages_target": spec.pages,
                "pages_total": current.pages_total,
                "finished": current.is_finished,
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        spec = _ReadSpec.parse(command)
        identity = identity_of(spec.item_ref)
        started = _view(before, identity)
        current = _view(latest, identity)
        if started is None or current is None:
            return None
        advanced = current.pages_read - started.pages_read
        return min(1.0, max(0.0, advanced / spec.pages))
