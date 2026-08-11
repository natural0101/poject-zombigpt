"""Deterministic literature selection.

Reading is the one need where the *goal* changes the answer completely: the
best item for boredom is the worst item for training carpentry. So the goal is
an explicit argument rather than something inferred, and every rejection names
the requirement the candidate failed — "Carpentry 3 needs level 0-2 and you are
4" is a product feature, not a debug aid. It is the sentence the voice
companion says.

One field here is tri-state and has to be, because the mod may or may not be
able to read it: ``unread_recipes``. Absent is *unknown* — not zero and not
some. Reading it as zero would say "it holds no recipe you have not already
learned" about a magazine nobody looked inside, which is the fabricated fact
this project treats as a defect; reading it as some would pick that magazine
over one that is honestly known to teach something. So absence refuses, with
its own sentence, and the recipe goal stays honest about what it could not see
(:func:`_filter_recipes`).

Blueprint: ``docs/blueprint/05_GAMEPLAY_SKILLS.md`` §5.4 and
``docs/blueprint/17_DECISION_TABLES.md`` §17.5.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..protocol import InventoryView, ItemView, JsonDict, PlayerState, ReasonCode
from .config import DEFAULT_POLICY_CONFIG, PolicyConfig
from .selection import (
    Rejection,
    RejectionReason,
    ScoreBreakdown,
    ScoredCandidate,
    ScoreFactor,
    bounded_rejections,
    is_user_reserved,
    needs_transfer_to_main,
    rank_candidates,
    reach_rejection,
    read_bool,
    read_float,
    read_int,
    read_str,
    truncation_note,
)

__all__ = [
    "SKILL_STAT_PREFIX",
    "TRAIT_ILLITERATE_STAT",
    "LiteratureChoice",
    "LiteratureGoal",
    "LiteratureGoalKind",
    "LiteratureKind",
    "LiteratureSelection",
    "LiteratureView",
    "select_literature",
    "skill_level",
]

#: ``PlayerState.stats`` is an open map, so the two keys this policy reads are
#: named here and nowhere else. They are the mod's side of the contract.
TRAIT_ILLITERATE_STAT: Final = "trait_illiterate"
SKILL_STAT_PREFIX: Final = "skill_"


class LiteratureKind(StrEnum):
    """What sort of reading matter an item is.

    The distinction is not cosmetic: a skill book has a level window, a recipe
    magazine is worthless once its recipes are known, and a newspaper is only
    ever worth reading for mood.
    """

    SKILL_BOOK = "skill_book"
    MAGAZINE = "magazine"
    NEWSPAPER = "newspaper"
    PRINT_MEDIA = "print_media"
    GENERIC = "generic"


class LiteratureGoalKind(StrEnum):
    """Why the character is reading (blueprint §5.4)."""

    REDUCE_BOREDOM = "reduce_boredom"
    LEARN_RECIPE = "learn_recipe"
    TRAIN_SKILL = "train_skill"
    READ_SPECIFIC = "read_specific"
    EXPLORE_LORE = "explore_lore"


#: Kinds that are outright wrong for a goal, as opposed to merely worse. Goals
#: absent from this map accept anything readable and let the score sort it out.
_REQUIRED_KINDS: Final[Mapping[LiteratureGoalKind, frozenset[LiteratureKind]]] = {
    LiteratureGoalKind.TRAIN_SKILL: frozenset({LiteratureKind.SKILL_BOOK}),
    LiteratureGoalKind.LEARN_RECIPE: frozenset(
        {LiteratureKind.MAGAZINE, LiteratureKind.PRINT_MEDIA}
    ),
}

#: Kinds a goal actively prefers; everything else readable still scores, lower.
_PREFERRED_KINDS: Final[Mapping[LiteratureGoalKind, frozenset[LiteratureKind]]] = {
    LiteratureGoalKind.REDUCE_BOREDOM: frozenset(
        {LiteratureKind.MAGAZINE, LiteratureKind.NEWSPAPER, LiteratureKind.GENERIC}
    ),
    LiteratureGoalKind.LEARN_RECIPE: frozenset({LiteratureKind.MAGAZINE}),
    LiteratureGoalKind.TRAIN_SKILL: frozenset({LiteratureKind.SKILL_BOOK}),
    LiteratureGoalKind.EXPLORE_LORE: frozenset(
        {LiteratureKind.PRINT_MEDIA, LiteratureKind.NEWSPAPER}
    ),
}

#: Score for a readable-but-not-preferred kind. Non-zero so that a stack of
#: skill books is still better than nothing when the goal is boredom.
_OFF_GOAL_MATCH: Final = 0.25


@dataclass(frozen=True, slots=True)
class LiteratureGoal:
    """What the reading is meant to achieve.

    ``skill`` and ``item_ref`` are validated against the kind at construction:
    a "train Carpentry" goal that forgot to say Carpentry would otherwise be
    silently answered with a novel.
    """

    kind: LiteratureGoalKind
    skill: str = ""
    item_ref: str = ""

    def __post_init__(self) -> None:
        if self.kind is LiteratureGoalKind.TRAIN_SKILL and not self.skill:
            raise ValueError("a train_skill goal must name the skill")
        if self.kind is LiteratureGoalKind.READ_SPECIFIC and not self.item_ref:
            raise ValueError("a read_specific goal must name the item")

    @classmethod
    def boredom(cls) -> LiteratureGoal:
        return cls(kind=LiteratureGoalKind.REDUCE_BOREDOM)

    @classmethod
    def train(cls, skill: str) -> LiteratureGoal:
        return cls(kind=LiteratureGoalKind.TRAIN_SKILL, skill=skill)

    @classmethod
    def recipe(cls) -> LiteratureGoal:
        return cls(kind=LiteratureGoalKind.LEARN_RECIPE)

    @classmethod
    def specific(cls, item_ref: str) -> LiteratureGoal:
        return cls(kind=LiteratureGoalKind.READ_SPECIFIC, item_ref=item_ref)


@dataclass(frozen=True, slots=True)
class LiteratureView:
    """Typed reading of :attr:`ItemView.literature`."""

    kind: LiteratureKind
    skill: str
    min_level: int
    max_level: int
    pages_total: int
    pages_read: int
    already_read: bool
    #: How many recipes in it the character has not learned, or ``None`` when
    #: the mod did not report the fact at all. The three values mean three
    #: different things and the policy keeps them apart; see the module
    #: docstring.
    unread_recipes: int | None
    boredom_change: float
    unhappy_change: float
    destroyed: bool

    @classmethod
    def from_item(cls, item: ItemView) -> LiteratureView | None:
        payload: Mapping[str, object] | None = item.literature
        if payload is None:
            return None
        pages_total = max(0, read_int(payload, "pages_total"))
        pages_read = max(0, read_int(payload, "pages_read"))
        return cls(
            kind=_classify(item, payload),
            skill=read_str(payload, "skill"),
            min_level=max(0, read_int(payload, "min_level")),
            # A book with no stated ceiling is useful at any level; using the
            # game's level cap keeps the comparison arithmetic total.
            max_level=read_int(payload, "max_level", _MAX_SKILL_LEVEL),
            pages_total=pages_total,
            pages_read=min(pages_read, pages_total) if pages_total else pages_read,
            already_read=read_bool(payload, "already_read"),
            unread_recipes=_read_unread_recipes(payload),
            boredom_change=read_float(payload, "boredom_change"),
            unhappy_change=read_float(payload, "unhappy_change"),
            destroyed=read_bool(payload, "destroyed"),
        )

    @property
    def pages_remaining(self) -> int:
        return max(0, self.pages_total - self.pages_read)

    @property
    def is_finished(self) -> bool:
        """True when there is nothing left in it to read."""
        if self.already_read:
            return True
        return self.pages_total > 0 and self.pages_remaining == 0

    @property
    def progress(self) -> float:
        """Share already read; 0.0 when the mod reports no page count."""
        if self.pages_total <= 0:
            return 0.0
        return self.pages_read / self.pages_total


#: The game's skill ceiling, used as the default upper bound of a book whose
#: payload gives none.
_MAX_SKILL_LEVEL: Final = 10


def _read_unread_recipes(payload: Mapping[str, object]) -> int | None:
    """The unread-recipe count, or ``None`` when the mod did not report one.

    Deliberately not :func:`~pz_agent_core.policy.selection.read_int`, whose
    whole job is to substitute a default: a default here would turn "the
    reader is missing on this build" into the confident claim that the
    magazine teaches nothing. A value present but not a number is the same
    fact — the count could not be read — so it lands on ``None`` too, and only
    a real number is believed.
    """
    value = payload.get("unread_recipes")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _classify(item: ItemView, payload: Mapping[str, object]) -> LiteratureKind:
    """Best available reading of what kind of literature this is.

    An explicit ``kind`` from the mod wins. Otherwise the type name is
    inspected, and a book that names a skill is a skill book — that is the
    property the reading rules actually turn on.
    """
    declared = read_str(payload, "kind").lower()
    for kind in LiteratureKind:
        if declared == kind.value:
            return kind
    haystack = f"{item.full_type} {item.category}".lower()
    if read_str(payload, "skill"):
        return LiteratureKind.SKILL_BOOK
    if "magazine" in haystack:
        return LiteratureKind.MAGAZINE
    if "newspaper" in haystack:
        return LiteratureKind.NEWSPAPER
    if "comic" in haystack or "photo" in haystack:
        return LiteratureKind.PRINT_MEDIA
    return LiteratureKind.GENERIC


def skill_level(player: PlayerState, skill: str) -> int:
    """The character's level in *skill*, or 0 when the mod did not report it.

    Matching is case-insensitive over a sorted key list so that two runs over
    the same observation always read the same value.
    """
    exact = player.stats.get(f"{SKILL_STAT_PREFIX}{skill}")
    if isinstance(exact, (int, float)) and not isinstance(exact, bool):
        return int(exact)
    wanted = f"{SKILL_STAT_PREFIX}{skill}".lower()
    for key in sorted(player.stats):
        if key.lower() != wanted:
            continue
        value = player.stats[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def is_illiterate(player: PlayerState) -> bool:
    """True when the character carries the Illiterate trait."""
    return bool(player.stats.get(TRAIT_ILLITERATE_STAT))


@dataclass(frozen=True, slots=True)
class LiteratureChoice:
    """The item to read, and why it is the right one for the goal."""

    item_ref: str
    display_name: str
    container_ref: str
    kind: LiteratureKind
    pages_remaining: int
    needs_transfer_to_main: bool
    rationale: str
    breakdown: ScoreBreakdown

    def as_dict(self) -> JsonDict:
        return {
            "item_ref": self.item_ref,
            "display_name": self.display_name,
            "container_ref": self.container_ref,
            "kind": self.kind.value,
            "pages_remaining": self.pages_remaining,
            "needs_transfer_to_main": self.needs_transfer_to_main,
            "rationale": self.rationale,
            "score": self.breakdown.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class LiteratureSelection:
    """A choice or a refusal, with the failed requirement for each candidate."""

    choice: LiteratureChoice | None
    reason_code: ReasonCode | None
    goal: LiteratureGoal
    rejections: tuple[Rejection, ...]
    ranked: tuple[ScoredCandidate, ...]
    #: Total candidates refused; ``rejections`` is a bounded sample of them.
    rejected_count: int = 0

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
        choice: LiteratureChoice,
        *,
        goal: LiteratureGoal,
        rejections: tuple[Rejection, ...],
        ranked: tuple[ScoredCandidate, ...],
        rejected_count: int,
    ) -> LiteratureSelection:
        return cls(
            choice=choice,
            reason_code=None,
            goal=goal,
            rejections=rejections,
            ranked=ranked,
            rejected_count=rejected_count,
        )

    @classmethod
    def refused(
        cls,
        goal: LiteratureGoal,
        rejections: tuple[Rejection, ...],
        rejected_count: int,
    ) -> LiteratureSelection:
        return cls(
            choice=None,
            reason_code=ReasonCode.NO_SUITABLE_LITERATURE,
            goal=goal,
            rejections=rejections,
            ranked=(),
            rejected_count=rejected_count,
        )

    def explain(self) -> str:
        if self.choice is not None:
            return self.choice.rationale
        if not self.rejections:
            return f"there is nothing to read for goal {self.goal.kind.value}"
        reasons = "; ".join(r.describe() for r in self.rejections)
        note = truncation_note(len(self.rejections), self.rejected_count)
        return f"nothing suitable to read for goal {self.goal.kind.value}: {reasons}{note}"

    def as_dict(self) -> JsonDict:
        return {
            "choice": None if self.choice is None else self.choice.as_dict(),
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "goal": {
                "kind": self.goal.kind.value,
                "skill": self.goal.skill,
                "item_ref": self.goal.item_ref,
            },
            "rejections": [r.as_dict() for r in self.rejections],
            "rejected_count": self.rejected_count,
            "ranked": [
                {"item_ref": c.item_ref, "score": c.score, "factors": c.breakdown.as_dict()}
                for c in self.ranked
            ],
        }


@dataclass(frozen=True, slots=True)
class _Context:
    config: PolicyConfig
    goal: LiteratureGoal
    illiterate: bool
    inventory: InventoryView
    player: PlayerState


def select_literature(
    inventory: InventoryView,
    player: PlayerState,
    goal: LiteratureGoal,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> LiteratureSelection:
    """Pick the best item to read for *goal*, or refuse with the reasons why.

    An Illiterate character rejects every candidate rather than short-circuiting
    to an empty refusal: the caller has to be able to say which books were in
    the bag and that the trait — not the shelf — is the problem.
    """
    context = _Context(
        config=config,
        goal=goal,
        illiterate=is_illiterate(player),
        inventory=inventory,
        player=player,
    )

    rejections: list[Rejection] = []
    candidates: list[tuple[ItemView, LiteratureView, ScoredCandidate]] = []
    for item in inventory.items:
        view = LiteratureView.from_item(item)
        if view is None:
            continue
        rejection = _first_rejection(item, view, context)
        if rejection is not None:
            rejections.append(rejection)
            continue
        breakdown = _score(item, view, context)
        candidates.append((item, view, ScoredCandidate(item.ref, item.display_name, breakdown)))

    reported = bounded_rejections(rejections, config)
    if not candidates:
        return LiteratureSelection.refused(goal, reported, len(rejections))

    ranked = rank_candidates(scored for _, _, scored in candidates)
    best_ref = ranked[0].item_ref
    item, view, scored = next(entry for entry in candidates if entry[0].ref == best_ref)
    choice = LiteratureChoice(
        item_ref=item.ref,
        display_name=item.display_name,
        container_ref=item.container_ref,
        kind=view.kind,
        pages_remaining=view.pages_remaining,
        needs_transfer_to_main=needs_transfer_to_main(inventory, item),
        rationale=_rationale(item, view, scored, context),
        breakdown=scored.breakdown,
    )
    return LiteratureSelection.chosen(
        choice,
        goal=goal,
        rejections=reported,
        ranked=ranked,
        rejected_count=len(rejections),
    )


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------
# Fixed order, most fundamental first: a trait the character cannot change, then
# the item's condition, then ownership, then whether it suits the stated goal.

_Filter = Callable[[ItemView, LiteratureView, _Context], Rejection | None]


def _reject(item: ItemView, reason: RejectionReason, detail: str) -> Rejection:
    return Rejection(
        item_ref=item.ref,
        display_name=item.display_name,
        reason=reason,
        detail=detail,
    )


def _filter_illiterate(
    item: ItemView, _view: LiteratureView, context: _Context
) -> Rejection | None:
    if not context.illiterate:
        return None
    return _reject(
        item,
        RejectionReason.ILLITERATE,
        "the character is Illiterate and cannot read anything",
    )


def _filter_destroyed(item: ItemView, view: LiteratureView, _context: _Context) -> Rejection | None:
    if not view.destroyed:
        return None
    return _reject(item, RejectionReason.DESTROYED, "the item is destroyed")


def _filter_user_reserved(
    item: ItemView, _view: LiteratureView, context: _Context
) -> Rejection | None:
    if not is_user_reserved(item, context.config):
        return None
    return _reject(
        item,
        RejectionReason.USER_RESERVED,
        "you marked it as reserved, so it is never taken automatically",
    )


def _filter_reach(item: ItemView, _view: LiteratureView, context: _Context) -> Rejection | None:
    return reach_rejection(context.inventory, item, context.config)


def _is_requested(item: ItemView, context: _Context) -> bool:
    """True when the user asked for this exact item by reference."""
    return (
        context.goal.kind is LiteratureGoalKind.READ_SPECIFIC and item.ref == context.goal.item_ref
    )


def _filter_already_read(
    item: ItemView, view: LiteratureView, context: _Context
) -> Rejection | None:
    """A finished book teaches nothing — unless the user named it.

    Re-reading is worthless in game, so it is refused by default. An explicit
    "read this one" is not the policy choosing to waste the character's time,
    it is the user's instruction, and refusing it would silently ignore a
    direct request.
    """
    if context.config.allow_reread or not view.is_finished or _is_requested(item, context):
        return None
    read_detail = (
        f"all {view.pages_total} pages are already read"
        if view.pages_total
        else "it has already been read"
    )
    return _reject(item, RejectionReason.ALREADY_READ, read_detail)


def _filter_skill_window(
    item: ItemView, view: LiteratureView, context: _Context
) -> Rejection | None:
    """Skill books only teach inside their level window."""
    if view.kind is not LiteratureKind.SKILL_BOOK or not view.skill:
        return None
    level = skill_level(context.player, view.skill)
    if level > view.max_level:
        return _reject(
            item,
            RejectionReason.SKILL_TOO_HIGH,
            f"it covers {view.skill} {view.min_level}-{view.max_level} and you are already "
            f"level {level}",
        )
    if level < view.min_level:
        return _reject(
            item,
            RejectionReason.SKILL_TOO_LOW,
            f"it needs {view.skill} level {view.min_level} and you are level {level}",
        )
    return None


def _filter_goal_skill(item: ItemView, view: LiteratureView, context: _Context) -> Rejection | None:
    if context.goal.kind is not LiteratureGoalKind.TRAIN_SKILL:
        return None
    if view.skill.lower() == context.goal.skill.lower():
        return None
    subject = view.skill or "no skill"
    return _reject(
        item,
        RejectionReason.WRONG_SKILL,
        f"it teaches {subject}, not {context.goal.skill}",
    )


def _filter_requested_item(
    item: ItemView, _view: LiteratureView, context: _Context
) -> Rejection | None:
    if context.goal.kind is not LiteratureGoalKind.READ_SPECIFIC:
        return None
    if item.ref == context.goal.item_ref:
        return None
    return _reject(
        item,
        RejectionReason.NOT_REQUESTED_ITEM,
        "you asked for a different item",
    )


def _filter_recipes(item: ItemView, view: LiteratureView, context: _Context) -> Rejection | None:
    """A recipe goal needs a magazine known to hold something new.

    Three inputs, three answers. A positive count passes. A zero is a real
    reading and refuses with the sentence it has always used. An *absent*
    count — the case that made this filter reject every item in the world,
    because nothing published the key — is the reader failing, not the
    magazine being exhausted, and it refuses with a sentence that says which
    of the two happened. Absence falling on the refusing side is the safe
    reading and the deliberate one: the item is not a confident pick, and a
    craft or a skill the character does not actually gain is a worse outcome
    than a book left unopened.

    Both refusals carry ``NO_UNREAD_RECIPES``. The shared vocabulary in
    :mod:`pz_agent_core.policy.selection` has no token for "unreadable", and
    adding one is a change to a module three policies share; until it exists
    the *detail* is what tells the two apart, and the detail is the half a
    user is read out loud.
    """
    if context.goal.kind is not LiteratureGoalKind.LEARN_RECIPE:
        return None
    if view.unread_recipes is None:
        return _reject(
            item,
            RejectionReason.NO_UNREAD_RECIPES,
            "this build does not report whether it still holds an unlearned recipe, "
            "so it cannot be picked as one that does",
        )
    if view.unread_recipes > 0:
        return None
    return _reject(
        item,
        RejectionReason.NO_UNREAD_RECIPES,
        "it holds no recipe you have not already learned",
    )


def _filter_kind(item: ItemView, view: LiteratureView, context: _Context) -> Rejection | None:
    required = _REQUIRED_KINDS.get(context.goal.kind)
    if required is None or view.kind in required:
        return None
    wanted = ", ".join(sorted(k.value for k in required))
    return _reject(
        item,
        RejectionReason.WRONG_KIND_FOR_GOAL,
        f"it is a {view.kind.value} and goal {context.goal.kind.value} needs {wanted}",
    )


_FILTERS: Final[tuple[_Filter, ...]] = (
    _filter_illiterate,
    _filter_destroyed,
    _filter_user_reserved,
    _filter_reach,
    _filter_already_read,
    _filter_kind,
    # Goal fit before the book's own level window: for a "train Carpentry"
    # goal, a maxed-out Cooking book is refused because it is not Carpentry,
    # which is the sentence the user needs, not "you are already level 10".
    _filter_goal_skill,
    _filter_skill_window,
    _filter_requested_item,
    _filter_recipes,
)


def _first_rejection(item: ItemView, view: LiteratureView, context: _Context) -> Rejection | None:
    for check in _FILTERS:
        rejection = check(item, view, context)
        if rejection is not None:
            return rejection
    return None


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _goal_match(view: LiteratureView, goal: LiteratureGoal) -> float:
    preferred = _PREFERRED_KINDS.get(goal.kind)
    if preferred is None or view.kind in preferred:
        return 1.0
    return _OFF_GOAL_MATCH


def _level_fit(view: LiteratureView, context: _Context) -> float:
    """Prefer the lowest-tier book the character has not outgrown.

    Reading a level 1-2 book at level 1 is worth more than starting a 4-6 one,
    because the low tiers are the ones that stop being useful soonest.
    """
    if view.kind is not LiteratureKind.SKILL_BOOK or not view.skill:
        return 0.0
    level = skill_level(context.player, view.skill)
    return 1.0 / (1.0 + max(0, view.max_level - level))


def _score(item: ItemView, view: LiteratureView, context: _Context) -> ScoreBreakdown:
    config = context.config
    transfer = needs_transfer_to_main(context.inventory, item)
    factors = (
        ScoreFactor(
            name="goal_match",
            raw=_goal_match(view, context.goal),
            weight=config.literature_weight_goal_match,
            detail=f"{view.kind.value} for goal {context.goal.kind.value}",
        ),
        ScoreFactor(
            name="progress",
            # Finish what is already started rather than opening a third book.
            raw=view.progress,
            weight=config.literature_weight_progress,
            detail=f"{view.pages_read}/{view.pages_total} pages read",
        ),
        ScoreFactor(
            name="boredom_relief",
            raw=max(0.0, -view.boredom_change) / config.boredom_reference,
            weight=config.literature_weight_boredom_relief,
        ),
        ScoreFactor(
            name="recipes",
            # An unreported count contributes nothing rather than a guess. It
            # is not a penalty either: for a boredom goal a magazine nobody
            # could look inside is still a magazine, and it should win or lose
            # on the factors that *were* readable.
            raw=(
                0.0
                if view.unread_recipes is None
                else min(1.0, view.unread_recipes / config.recipe_reference)
            ),
            weight=config.literature_weight_recipes,
            detail=(
                "unread recipes not reported"
                if view.unread_recipes is None
                else f"{view.unread_recipes} unread recipes"
            ),
        ),
        ScoreFactor(
            name="level_fit",
            raw=_level_fit(view, context),
            weight=config.literature_weight_level_fit,
        ),
        ScoreFactor(
            name="weight",
            raw=-item.weight / config.weight_reference,
            weight=config.literature_weight_weight,
        ),
        ScoreFactor(
            name="transfer",
            raw=-1.0 if transfer else 0.0,
            weight=config.literature_weight_transfer,
            detail="has to be moved to the main inventory" if transfer else "already in hand",
        ),
    )
    return ScoreBreakdown(factors)


def _rationale(
    item: ItemView,
    view: LiteratureView,
    scored: ScoredCandidate,
    context: _Context,
) -> str:
    parts = [
        f"{item.display_name} scores {scored.score:.2f} ({scored.breakdown.explain()})",
        f"a {view.kind.value} for goal {context.goal.kind.value}",
    ]
    if view.skill:
        parts.append(
            f"{view.skill} {view.min_level}-{view.max_level}, "
            f"you are level {skill_level(context.player, view.skill)}"
        )
    if view.pages_total:
        parts.append(f"{view.pages_remaining} of {view.pages_total} pages left")
    if view.unread_recipes:
        parts.append(f"{view.unread_recipes} unread recipes")
    if needs_transfer_to_main(context.inventory, item):
        parts.append("it has to be moved to the main inventory first")
    return "; ".join(parts)
