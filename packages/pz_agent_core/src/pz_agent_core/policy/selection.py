"""The vocabulary the three selection policies share.

Food, drink and literature answer the same shape of question: which item, why
that one, and — when the answer is "none" — why every single candidate was
turned down. The refusal half matters as much as the choice: ``pz-agent
doctor`` and the voice companion both have to be able to say *why* there is
nothing to eat, so a rejection is a first-class value here rather than a log
line thrown away inside a filter.

Invariant: everything in this module is a pure function of its arguments. No
clock, no random source, no filesystem, and every ranking ends in a total order
whose last key is the item reference — so shuffling the inventory cannot change
which item wins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from ..protocol import CapabilityState, ContainerKind, InventoryView, ItemView, JsonDict
from .config import PolicyConfig

__all__ = [
    "NO_CAPABILITIES",
    "SCORE_PRECISION",
    "CapabilityLookup",
    "CapabilitySnapshot",
    "Rejection",
    "RejectionReason",
    "ScoreBreakdown",
    "ScoreFactor",
    "ScoredCandidate",
    "bounded_rejections",
    "count_by_full_type",
    "is_strategic_reserve",
    "is_user_reserved",
    "needs_transfer_to_main",
    "rank_candidates",
    "reach_rejection",
    "read_bool",
    "read_float",
    "read_int",
    "read_str",
    "read_str_tuple",
]

#: Scores are compared at this many decimal places. Two candidates whose totals
#: differ only below it are a genuine tie and fall through to the reference
#: tiebreak, which keeps the order stable against the last bit of float noise.
SCORE_PRECISION: Final = 6


class RejectionReason(StrEnum):
    """Why one candidate was refused.

    One closed vocabulary across all three policies: a refusal is rendered to
    the user and written to a log, and a caller that wants to react to "there
    is food but it is all rotten" needs to match on something stable.
    """

    # --- kind mismatch -----------------------------------------------------
    NOT_FOOD = "NOT_FOOD"
    NOT_DRINKABLE = "NOT_DRINKABLE"
    NOT_LITERATURE = "NOT_LITERATURE"

    # --- condition ---------------------------------------------------------
    NOT_EDIBLE = "NOT_EDIBLE"
    DESTROYED = "DESTROYED"
    ROTTEN = "ROTTEN"
    RAW_UNSAFE = "RAW_UNSAFE"
    BURNT = "BURNT"
    POISONOUS = "POISONOUS"
    TAINTED = "TAINTED"
    FROZEN = "FROZEN"
    REQUIRES_COOKING = "REQUIRES_COOKING"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    NO_HUNGER_RELIEF = "NO_HUNGER_RELIEF"
    EMPTY_CONTAINER = "EMPTY_CONTAINER"
    NO_THIRST_RELIEF = "NO_THIRST_RELIEF"
    ALCOHOL_NOT_PERMITTED = "ALCOHOL_NOT_PERMITTED"

    # --- ownership and reach ----------------------------------------------
    USER_RESERVED = "USER_RESERVED"
    STRATEGIC_RESERVE = "STRATEGIC_RESERVE"
    LAST_CONTAINER_RESERVE = "LAST_CONTAINER_RESERVE"
    CONTAINER_UNREACHABLE = "CONTAINER_UNREACHABLE"

    # --- literature suitability -------------------------------------------
    ILLITERATE = "ILLITERATE"
    ALREADY_READ = "ALREADY_READ"
    SKILL_TOO_LOW = "SKILL_TOO_LOW"
    SKILL_TOO_HIGH = "SKILL_TOO_HIGH"
    WRONG_SKILL = "WRONG_SKILL"
    WRONG_KIND_FOR_GOAL = "WRONG_KIND_FOR_GOAL"
    NO_UNREAD_RECIPES = "NO_UNREAD_RECIPES"
    NOT_REQUESTED_ITEM = "NOT_REQUESTED_ITEM"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One candidate and the single filter that stopped it.

    Only the *first* matching filter is recorded. Filters run in a fixed,
    documented order per policy, so the reported reason is deterministic and
    reads as the most fundamental problem with the item rather than an
    arbitrary one of several.
    """

    item_ref: str
    display_name: str
    reason: RejectionReason
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a rejection must explain itself; detail is empty")

    def as_dict(self) -> dict[str, str]:
        return {
            "item_ref": self.item_ref,
            "display_name": self.display_name,
            "reason": self.reason.value,
            "detail": self.detail,
        }

    def describe(self) -> str:
        """One line for a user-facing explanation."""
        return f"{self.display_name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ScoreFactor:
    """One term of a score, kept separate so the total can be explained.

    ``weight`` is always non-negative and ``raw`` carries the sign, so a
    penalty is visible as a negative raw value instead of being hidden in a
    negative weight buried in the config.
    """

    name: str
    raw: float
    weight: float
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a score factor needs a name")
        if self.weight < 0.0:
            raise ValueError(f"factor {self.name}: weight must be non-negative, got {self.weight}")

    @property
    def contribution(self) -> float:
        return self.raw * self.weight

    def as_dict(self) -> JsonDict:
        out: JsonDict = {
            "name": self.name,
            "raw": self.raw,
            "weight": self.weight,
            "contribution": self.contribution,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The factors behind one candidate's score.

    The policies return this instead of a bare number because "why did it pick
    that?" is a question a user asks out loud, and a float cannot answer it.
    """

    factors: tuple[ScoreFactor, ...]

    def __post_init__(self) -> None:
        names = [f.name for f in self.factors]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate score factor names: {sorted(names)}")

    @property
    def total(self) -> float:
        return sum(f.contribution for f in self.factors)

    def factor(self, name: str) -> ScoreFactor | None:
        return next((f for f in self.factors if f.name == name), None)

    def as_dict(self) -> JsonDict:
        return {
            "total": self.total,
            "factors": [f.as_dict() for f in self.factors],
        }

    def explain(self, limit: int = 3) -> str:
        """The *limit* factors that moved the total most, largest first."""
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        ordered = sorted(self.factors, key=lambda f: (-abs(f.contribution), f.name))
        parts = [f"{f.name} {f.contribution:+.2f}" for f in ordered[:limit] if f.contribution]
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """An item that survived every hard filter, with its score."""

    item_ref: str
    display_name: str
    breakdown: ScoreBreakdown

    @property
    def score(self) -> float:
        return self.breakdown.total


def rank_candidates(candidates: Iterable[ScoredCandidate]) -> tuple[ScoredCandidate, ...]:
    """Order candidates best-first with a total, input-order-independent order.

    The tiebreak on ``item_ref`` is what makes the policy deterministic: item
    references are unique within a session, so no two candidates can compare
    equal and the sort cannot fall back on the order the mod happened to
    enumerate the inventory in.
    """
    return tuple(sorted(candidates, key=lambda c: (-round(c.score, SCORE_PRECISION), c.item_ref)))


@runtime_checkable
class CapabilityLookup(Protocol):
    """Read-only view of the runtime capability probes.

    Structural on purpose: the capability subsystem owns the real report, and
    the policies only ever ask it two questions. Anything that answers those
    two questions works here, including the snapshot below.
    """

    def state(self, probe: str) -> CapabilityState:
        """Probe result, or ``UNSUPPORTED`` when the probe never ran."""
        ...

    def is_usable(self, probe: str) -> bool:
        """True when a feature backed by *probe* may be used."""
        ...


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """An immutable set of probe results.

    An unprobed capability reports ``UNSUPPORTED`` rather than an optimistic
    guess: the whole point of the capability system is that nothing is assumed
    to exist because a wiki said so.

    Build one with :meth:`from_mapping`; it copies and freezes the input, so a
    caller that keeps mutating its own dict cannot change a policy decision
    that has already been made.
    """

    probes: Mapping[str, CapabilityState] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, probes: Mapping[str, CapabilityState]) -> CapabilitySnapshot:
        """Copy *probes* so a later mutation by the caller cannot leak in."""
        return cls(probes=MappingProxyType(dict(probes)))

    @classmethod
    def verified(cls, *names: str) -> CapabilitySnapshot:
        """Snapshot in which exactly *names* are verified. Handy for tests."""
        return cls.from_mapping({name: CapabilityState.VERIFIED for name in names})

    def state(self, probe: str) -> CapabilityState:
        return self.probes.get(probe, CapabilityState.UNSUPPORTED)

    def is_usable(self, probe: str) -> bool:
        return self.state(probe).usable


#: What a policy sees when the caller has no capability information at all.
NO_CAPABILITIES: Final = CapabilitySnapshot()


# --------------------------------------------------------------------------
# total readers over the mod's untyped sub-objects
# --------------------------------------------------------------------------
# ``ItemView.food``/``literature``/``fluid`` are open mappings straight off the
# wire. They are data from the game, which the working agreement classes as
# untrusted, so every reader below is total: a missing key, a null, or a value
# of the wrong type yields the documented default instead of an exception.


def read_bool(payload: Mapping[str, Any] | None, key: str, default: bool = False) -> bool:
    if payload is None:
        return default
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return default


def read_float(payload: Mapping[str, Any] | None, key: str, default: float = 0.0) -> float:
    if payload is None:
        return default
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def read_int(payload: Mapping[str, Any] | None, key: str, default: int = 0) -> int:
    if payload is None:
        return default
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def read_str(payload: Mapping[str, Any] | None, key: str, default: str = "") -> str:
    if payload is None:
        return default
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def read_str_tuple(payload: Mapping[str, Any] | None, key: str) -> tuple[str, ...]:
    if payload is None:
        return ()
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str))


# --------------------------------------------------------------------------
# shared item predicates
# --------------------------------------------------------------------------


def _lowered_tags(item: ItemView) -> frozenset[str]:
    return frozenset(tag.lower() for tag in item.tags)


def is_user_reserved(item: ItemView, config: PolicyConfig) -> bool:
    """True when the user marked this item as not-for-automatic-use.

    Never overridden by need: the master prompt's behavioural limits forbid
    consuming a reserved or favourite item without explicit permission, and a
    starving character is exactly when a player would be angriest about it.
    """
    if config.treat_favorite_as_reserved and item.favorite:
        return True
    if read_bool(item.extra, "reserved"):
        return True
    return bool(_lowered_tags(item) & config.user_reserve_tags)


def is_strategic_reserve(item: ItemView, config: PolicyConfig) -> bool:
    """True for supplies the user set aside for an emergency.

    Unlike :func:`is_user_reserved` this one *is* overridden — by the need
    actually becoming the emergency it was saved for.
    """
    if read_bool(item.extra, "strategic_reserve"):
        return True
    return bool(_lowered_tags(item) & config.strategic_reserve_tags)


def count_by_full_type(items: Iterable[ItemView]) -> dict[str, int]:
    """How many of each item type the inventory holds; the scarcity input."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.full_type] = counts.get(item.full_type, 0) + 1
    return counts


def needs_transfer_to_main(inventory: InventoryView, item: ItemView) -> bool:
    """True when the item must be moved before it can be used.

    An item whose container the mod did not report counts as needing a move:
    "we do not know where it is" is not "it is in hand".
    """
    container = inventory.container(item.container_ref)
    if container is None:
        return True
    return container.kind is not ContainerKind.PLAYER_MAIN


def reach_rejection(
    inventory: InventoryView,
    item: ItemView,
    config: PolicyConfig,
) -> Rejection | None:
    """Reject items the policy cannot honestly promise to reach."""
    container = inventory.container(item.container_ref)
    if container is None:
        return Rejection(
            item_ref=item.ref,
            display_name=item.display_name,
            reason=RejectionReason.CONTAINER_UNREACHABLE,
            detail=(
                f"the mod did not report the container {item.container_ref}, "
                "so the item cannot be located"
            ),
        )
    if not container.accessible:
        return Rejection(
            item_ref=item.ref,
            display_name=item.display_name,
            reason=RejectionReason.CONTAINER_UNREACHABLE,
            detail=f"{container.name} is not accessible right now",
        )
    on_person = container.kind in {
        ContainerKind.PLAYER_MAIN,
        ContainerKind.CARRIED,
        ContainerKind.WORN,
    }
    if not on_person and not config.allow_world_containers:
        return Rejection(
            item_ref=item.ref,
            display_name=item.display_name,
            reason=RejectionReason.CONTAINER_UNREACHABLE,
            detail=(
                f"{container.name} is a {container.kind.value} container and policy keeps "
                "selection to what the character is carrying"
            ),
        )
    return None


def bounded_rejections(
    rejections: Iterable[Rejection],
    config: PolicyConfig,
) -> tuple[Rejection, ...]:
    """Trim the rejection list to the configured bound, order preserved."""
    return tuple(rejections)[: config.max_reported_rejections]
