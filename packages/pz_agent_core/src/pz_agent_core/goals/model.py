"""The typed goal channel's vocabulary: a closed set of kinds, bounded params.

A goal is the widest thing anything outside the deterministic core is allowed to
express. AGENTS.md states the rule for the model — "the model may express a
*goal*; it never picks the sandwich" — and the voice package states it for a
microphone: a transcript "selects one of these tokens or it selects nothing".
This module is where that promise stops being a convention and becomes a type.

Three properties hold here, and each of them is checked rather than documented:

* **Closed.** :class:`GoalKind` and :class:`TrainableSkill` are enums, and the
  only route from a string to either of them is :func:`parse_kind` /
  :func:`parse_skill`, which resolve against the enum or return ``None``. There
  is no field anywhere in this module that carries free text into the core. The
  one caller-supplied string that exists — the idempotency key — is a *handle*:
  it is shape-checked, never interpreted, never stored on a public object and
  never rendered, because a refusal that echoed it would put a caller's bytes in
  a traceback.
* **Bounded.** Every numeric parameter has a range in :data:`NUMERIC_RANGES` and
  is checked against it at construction. Every goal carries a
  :class:`GoalBudget` — wall-clock *and* step count *and* a pending time to live
  — so "the goal eventually ends" is arithmetic rather than hope. The one
  free-form string on the surface, a record's ``detail``, is capped at
  :data:`MAX_DETAIL_CHARS` and must be a single printable line, because it is
  read back into a log line and a line break in it is a line whose contents
  somebody else chose.
* **Honest.** :class:`GoalRecord` refuses to exist in the ``SUCCEEDED`` state
  without observed postcondition evidence, exactly as
  :meth:`~pz_agent_core.protocol.messages.ActionResult.succeeded` does one layer
  down. The queue routes success through an ``ActionResult`` so there is only
  one place in the process that can mint the claim.

The kind set is deliberately identical to
:class:`pz_agent_core.planner.provider.GoalKind`: :func:`to_planner_goal` is a
*total* mapping, so no goal can be admitted to the channel that the
deterministic planner has no way to serve. A kind that could be submitted and
never routed would be a stub wearing an enum member's clothes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..actions.adapters.literature import MAX_READ_PAGES
from ..actions.adapters.survival import MAX_REST_TARGET, MIN_REST_TARGET, MIN_SLEEP_HOURS
from ..loot import LootCategory
from ..planner.provider import Goal as PlannerGoal
from ..planner.provider import GoalKind as PlannerGoalKind
from ..protocol import ReasonCode

__all__ = [
    "DEFAULT_BUDGETS",
    "GOAL_SPECS",
    "MAX_DETAIL_CHARS",
    "MAX_EVIDENCE_KEYS",
    "MAX_GOAL_STEPS",
    "MAX_GOAL_WALL_MS",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_LOOT_CATEGORIES_CHARS",
    "MAX_LOOT_RADIUS",
    "MAX_PARSED_TOKEN_CHARS",
    "MAX_PENDING_TTL_MS",
    "MAX_RENDERED_VALUE_CHARS",
    "MAX_SKILL_LEVEL",
    "MAX_SLEEP_GOAL_HOURS",
    "MAX_TARGET_FLOOR",
    "MAX_TARGET_SQUARE",
    "MIN_GOAL_WALL_MS",
    "MIN_LOOT_RADIUS",
    "MIN_TARGET_FLOOR",
    "NUMERIC_RANGES",
    "PARAM_NAMES",
    "TERMINAL_GOAL_STATES",
    "AreaScope",
    "GoalAdmission",
    "GoalBudget",
    "GoalKind",
    "GoalParams",
    "GoalRecord",
    "GoalRefusal",
    "GoalRequest",
    "GoalSpec",
    "GoalState",
    "GoalTransition",
    "LootScope",
    "NumericRange",
    "TrainableSkill",
    "key_digest",
    "mint_goal_id",
    "normalise_evidence_keys",
    "parse_kind",
    "parse_loot_categories",
    "parse_scope",
    "parse_skill",
    "to_planner_goal",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Longest idempotency key accepted. The key is never stored or rendered, so
#: the bound is about the work done on it (hashing, shape matching) rather than
#: about a wire limit.
MAX_IDEMPOTENCY_KEY_LEN: Final = 64

#: Keys are minted by callers, so they are checked for shape and refused when
#: they fail rather than sanitised — sanitising two distinct keys into one would
#: silently merge two goals.
#:
#: Anchored with ``\Z`` and not with ``$``: ``$`` also matches immediately before
#: a trailing newline, so ``"order-42\n"`` would pass a ``$``-anchored pattern,
#: hash to a different digest than ``"order-42"``, and become a second goal from
#: what the caller reads as one key. The same anchor for the same reason appears
#: on :data:`_EVIDENCE_KEY_SHAPE` below.
_KEY_SHAPE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}\Z")

#: Longest string :func:`parse_kind` and :func:`parse_skill` will look at. Both
#: are fed from transcripts and model output; matching is a dict lookup, but the
#: case fold in front of it is linear, so the input is bounded first.
MAX_PARSED_TOKEN_CHARS: Final = 64

#: Ceiling on a goal's step budget. Larger than
#: :data:`pz_agent_core.planner.plan.MAX_PLAN_STEPS` (5) and deliberately not
#: equal to it: one goal may be served by more than one plan — a plan that runs
#: out is re-planned against the world as it now is — so a ceiling of exactly one
#: plan's length would end a goal that was making progress. It is a small
#: multiple of that length rather than an open number, because the steps a goal
#: may spend have to be countable by the executor that dispatches them; the
#: relationship is pinned by a test rather than left to this comment.
MAX_GOAL_STEPS: Final = 12

#: Wall-clock ceiling on one goal, and the floor below which a budget is not a
#: budget but an immediate expiry.
MAX_GOAL_WALL_MS: Final = 900_000
MIN_GOAL_WALL_MS: Final = 1_000

#: How long a goal may sit un-activated before the channel gives up on it. Its
#: existence is what makes "every goal reaches a terminal state" true of a goal
#: that was admitted and never started.
MAX_PENDING_TTL_MS: Final = 600_000

#: The game's skill ceiling.
MAX_SKILL_LEVEL: Final = 10

#: The largest world-square coordinate a navigation target may name. The
#: movement adapter itself bounds only the *shape* of a coordinate (an integer
#: square; ``read_position``) and leaves "does that square exist" to the mod,
#: which refuses a square that is not loaded. This ceiling therefore guards the
#: arithmetic, not the geography: it comfortably contains the Build 42 world
#: grid while keeping the number ordinary enough to quote in a refusal.
MAX_TARGET_SQUARE: Final = 32_000

#: The floors a navigation target may name. Build 42's world stacks basements
#: below ground and storeys above it; both directions are finite, and a target
#: outside them is a typo to refuse rather than a journey to attempt.
MIN_TARGET_FLOOR: Final = -32
MAX_TARGET_FLOOR: Final = 31

#: The Chebyshev radius a ``loot_area`` goal with ``scope=radius`` may sweep,
#: in squares around the position the goal was activated at. The ceiling is
#: the single-move distance the mod accepts (``MAX_MOVE_DISTANCE_SQUARES``,
#: value pinned by a test rather than imported — the goal channel does not
#: depend on the action layer's modules): a wider sweep than the character can
#: cross in one bounded move is a patrol, not "loot this area", and the
#: autonomous-radius rule in AGENTS.md is exactly about not admitting one.
MIN_LOOT_RADIUS: Final = 1
MAX_LOOT_RADIUS: Final = 30

#: Longest ``categories`` string a ``loot_area`` goal may carry. The string is
#: a comma-joined list of closed tokens — every :class:`LootCategory` value
#: fits several times over — so the bound is about the parsing work, not about
#: expressiveness; anything longer holds no set of categories this build files
#: loot under.
MAX_LOOT_CATEGORIES_CHARS: Final = 128

#: The longest night a ``sleep_until_rested`` goal may ask for. Deliberately
#: narrower than the sleep adapter's own ``MAX_SLEEP_HOURS`` (16): the kind's
#: whole meaning is "until rested", and a request for more than half a day of
#: sleep is a typo to refuse at the door rather than a night to attempt. The
#: adapter keeps its wider bound for callers that drive the action directly;
#: the floor and the rest target's bounds are the adapters' own, imported
#: rather than restated so the two layers cannot drift apart.
MAX_SLEEP_GOAL_HOURS: Final = 12

#: Postcondition field *names* kept on a finished goal. Values are deliberately
#: not kept: evidence values are forwarded from the mod, which forwards them
#: from the game, and game-authored text is untrusted data (AGENTS.md).
MAX_EVIDENCE_KEYS: Final = 8

#: Shape a protocol evidence key has. Anything else is recorded under
#: :data:`_UNNAMED_EVIDENCE_KEY` rather than carried through. A key arrives from
#: the mod, which forwarded it from the game, so a ``$`` anchor here would let
#: ``"hunger\n"`` through and put a game-chosen line break inside a record that
#: a log line is built from.
_EVIDENCE_KEY_SHAPE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,39}\Z")
_UNNAMED_EVIDENCE_KEY: Final = "unnamed"

#: Shape :func:`key_digest` produces, restated so :class:`GoalRecord` can refuse
#: a record built without one. A goal with no digest is a goal no resubmission
#: can ever resolve to, which is the whole of what the idempotency key buys.
_DIGEST_SHAPE: Final = re.compile(r"^[0-9a-f]{16}\Z")

#: Longest diagnostic a record or a transition carries. The text is assembled by
#: this process from constants and numbers — it is not caller text and must not
#: become caller text — so the bound is a guard rail rather than a budget, and an
#: over-long or multi-line detail is refused rather than truncated: truncating
#: would leave a caller believing a log line said something it did not.
MAX_DETAIL_CHARS: Final = 200

#: Longest rendering of a *caller-supplied number* any refusal in this module
#: will carry. Quoting the offending value is what makes a range refusal useful,
#: but a Python ``int`` has no width: ``f"{value}"`` is a message whose length
#: the caller chooses, and past CPython's 4300-digit conversion limit it is not
#: this module's message at all — ``str(int)`` raises, and what the caller
#: receives is the interpreter advising them to raise
#: ``sys.set_int_max_str_digits``. Both outcomes break the rule every refusal
#: here is built to keep: say what failed and what to do, in one bounded line.
MAX_RENDERED_VALUE_CHARS: Final = 26

#: Magnitude past which an ``int`` is described rather than quoted. *Compared*
#: against, never converted, so an arbitrarily large value costs nothing and
#: never reaches ``str``. Nineteen digits is as wide as an ``int`` below it can
#: print, which is what keeps the rendering inside its bound.
_MAX_RENDERED_MAGNITUDE: Final = 10**18

_UNQUOTABLE_VALUE: Final = "a value too large to quote"


def _render_value(value: float) -> str:
    """Render *value* for a refusal, without letting the caller pick its length.

    Bounded by :data:`MAX_RENDERED_VALUE_CHARS`, and bounded by *construction*
    rather than by a truncation nobody could reach: an ``int`` outside
    ±10\\ :sup:`18` is described instead of quoted, and everything still
    admissible is either a shorter ``int`` (at most 19 digits and a sign) or a
    ``float``, whose widest repr — ``-1.7976931348623157e+308`` — is 24
    characters. A length check after the fact would be a branch no input can
    take; the bound is pinned by a test that walks the extremes instead.
    """
    if isinstance(value, int) and not -_MAX_RENDERED_MAGNITUDE < value < _MAX_RENDERED_MAGNITUDE:
        return _UNQUOTABLE_VALUE
    return f"{value}"


# --------------------------------------------------------------------------
# closed vocabularies
# --------------------------------------------------------------------------


class GoalKind(StrEnum):
    """Everything the goal channel will carry.

    Closed, and closed for a specific reason: this is the whole width of the
    opening between a microphone or a language model and the action engine. A
    new member is a reviewed change to :data:`GOAL_SPECS`, to
    :data:`DEFAULT_BUDGETS` and to :data:`_PLANNER_KIND` together, and the tests
    fail until all three know about it.

    ``NAVIGATE_TO``, ``LOOT_AREA``, ``RETURN_HOME`` and ``EXPLORE_AREA`` are
    the kinds not served by a plan provider: the sidecar's deterministic route
    executor (``pz_agent_core.navigation``) walks the first square by square,
    the CLI's deterministic loot mission (``pz_agent_cli.loot_mission``)
    drives the second container by container, ``RETURN_HOME`` is one journey
    to the square the save's memory remembers as home, and ``EXPLORE_AREA``
    is the CLI's deterministic explore mission
    (``pz_agent_cli.explore_mission``), driven frontier square by frontier
    square. :class:`~pz_agent_core.planner.provider.NullProvider` refuses all
    four by name rather than approximating them with a plan. The mapping to
    the planner vocabulary stays total all the same, because these goals
    still cross the planner seam on their way to the deterministic server.

    ``TREAT_WOUNDS``, ``REST_UNTIL`` and ``SLEEP_UNTIL_RESTED`` join that
    arrangement one wave later, served by the CLI's deterministic care
    missions (``pz_agent_cli.care_mission``) over the existing medical and
    survival adapters: bandage every observed bleeding wound, one
    ``survival.rest`` to a target the adapter itself verifies, one
    ``survival.sleep`` whose danger refusal surfaces unchanged.
    :class:`~pz_agent_core.planner.provider.NullProvider` refuses all three
    by name, exactly as it refuses the four above.
    """

    SATISFY_HUNGER = "satisfy_hunger"
    SATISFY_THIRST = "satisfy_thirst"
    READ_FOR_BOREDOM = "read_for_boredom"
    TRAIN_SKILL = "train_skill"
    LEARN_RECIPE = "learn_recipe"
    NAVIGATE_TO = "navigate_to"
    LOOT_AREA = "loot_area"
    RETURN_HOME = "return_home"
    EXPLORE_AREA = "explore_area"
    TREAT_WOUNDS = "treat_wounds"
    REST_UNTIL = "rest_until"
    SLEEP_UNTIL_RESTED = "sleep_until_rested"


class LootScope(StrEnum):
    """What "the area" means to a ``loot_area`` or ``explore_area`` goal.

    Closed for the same reason every other vocabulary here is: the scope is
    the widest thing about the goal — it decides how far the character may
    wander unattended — so it is a reviewed token, never a string. ``ROOM``
    and ``BUILDING`` are pinned from the *current* observation at activation
    (the room or building the character stands in as the build reports it);
    ``RADIUS`` pins the activation square and sweeps a Chebyshev radius of
    :data:`NUMERIC_RANGES`'s ``radius`` squares around it. A build whose room
    reader is unavailable refuses ``ROOM`` and ``BUILDING`` with a typed
    failure naming ``RADIUS`` as the alternative — it never guesses.

    The name keeps the loot epic's spelling although two kinds now share the
    vocabulary; :data:`AreaScope` below is the same enum under the neutral
    name, so new call sites need not pretend they are looting. One enum, not
    two: a second scope vocabulary would be two definitions of how far the
    character may wander, and the one that drifted would win somewhere.
    """

    ROOM = "room"
    BUILDING = "building"
    RADIUS = "radius"


#: The scope vocabulary under its kind-neutral name — the very same enum, so
#: ``AreaScope.RADIUS is LootScope.RADIUS`` and no import ever has to choose
#: which of two vocabularies is authoritative.
AreaScope = LootScope


class TrainableSkill(StrEnum):
    """The skills a ``train_skill`` goal may name.

    :class:`pz_agent_core.planner.provider.Goal` takes the skill as a ``str``,
    which is right for it — it is handed one by policy code that already knows
    the book's own skill string. It is wrong for a channel fed by speech, so the
    channel narrows it to this enum and :func:`to_planner_goal` widens it back.

    The values are lower-case because
    :func:`pz_agent_core.policy.literature.select_literature` compares a goal's
    skill to a book's case-insensitively; the casing here is therefore not
    load-bearing for selection, only for the wire.
    """

    CARPENTRY = "carpentry"
    COOKING = "cooking"
    FARMING = "farming"
    ELECTRICAL = "electrical"
    METALWORKING = "metalworking"
    MECHANICS = "mechanics"
    TAILORING = "tailoring"
    FORAGING = "foraging"
    FISHING = "fishing"
    TRAPPING = "trapping"
    FIRST_AID = "first_aid"


class GoalState(StrEnum):
    """Where a goal is in its life.

    ``EXPIRED`` is separated from ``FAILED`` because the two say different
    things to a user: something went wrong versus the goal ran out of the budget
    it was given without anyone observing it finish.
    """

    PENDING = "pending"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_GOAL_STATES


TERMINAL_GOAL_STATES: Final[frozenset[GoalState]] = frozenset(
    {GoalState.SUCCEEDED, GoalState.FAILED, GoalState.CANCELLED, GoalState.EXPIRED}
)


def parse_kind(raw: str) -> GoalKind | None:
    """Resolve *raw* to a :class:`GoalKind`, or to nothing.

    One of the two doors in this package through which a string may enter, and
    it opens onto an enum member or onto ``None`` — never onto the string
    itself. Callers report the ``None`` case with a refusal that does not quote
    *raw*.
    """
    return _KIND_BY_VALUE.get(raw[:MAX_PARSED_TOKEN_CHARS].strip().casefold())


def parse_skill(raw: str) -> TrainableSkill | None:
    """Resolve *raw* to a :class:`TrainableSkill`, or to nothing.

    Spaces and hyphens fold to the underscore the enum uses so that "first aid"
    and "first-aid" reach the same member; nothing else about *raw* survives the
    call.
    """
    token = raw[:MAX_PARSED_TOKEN_CHARS].strip().casefold().replace(" ", "_").replace("-", "_")
    return _SKILL_BY_VALUE.get(token)


def parse_scope(raw: str) -> LootScope | None:
    """Resolve *raw* to a :class:`LootScope`, or to nothing.

    The third door of the same shape as :func:`parse_kind`: bounded first,
    folded once, resolved against the enum or answered with ``None`` — never
    with the string. Callers report the ``None`` case without quoting *raw*.
    """
    return _SCOPE_BY_VALUE.get(raw[:MAX_PARSED_TOKEN_CHARS].strip().casefold())


def parse_loot_categories(raw: str) -> frozenset[LootCategory]:
    """The categories a ``loot_area`` goal's ``categories`` string selects.

    The string is a comma-joined list of closed tokens because
    :class:`GoalParams` carries scalars, not collections; this function is the
    one place the join is undone, so the channel and the mission cannot come
    to read it differently. Tokens resolve case-insensitively against the
    :class:`~pz_agent_core.loot.LootCategory` values and against nothing else.

    Raises:
        ValueError: the string is longer than
            :data:`MAX_LOOT_CATEGORIES_CHARS`, holds an empty or repeated
            token, or names a token that is not a category this build files
            loot under. No refusal quotes the offending token — the string
            reaches this function from a caller the channel does not trust
            with a traceback.
    """
    if len(raw) > MAX_LOOT_CATEGORIES_CHARS:
        raise ValueError(f"categories must be at most {MAX_LOOT_CATEGORIES_CHARS} characters")
    selected: set[LootCategory] = set()
    for token in raw.split(","):
        member = _CATEGORY_BY_VALUE.get(token.strip().casefold())
        if member is None:
            raise ValueError(
                "categories must be a comma-joined list of loot category tokens "
                f"({', '.join(sorted(_CATEGORY_BY_VALUE))}); one entry is not one of them"
            )
        if member in selected:
            raise ValueError("categories must not repeat a category token")
        selected.add(member)
    return frozenset(selected)


_KIND_BY_VALUE: Final[Mapping[str, GoalKind]] = MappingProxyType({k.value: k for k in GoalKind})
_SKILL_BY_VALUE: Final[Mapping[str, TrainableSkill]] = MappingProxyType(
    {s.value: s for s in TrainableSkill}
)
_SCOPE_BY_VALUE: Final[Mapping[str, LootScope]] = MappingProxyType(
    {scope.value: scope for scope in LootScope}
)
#: Keys are the case-folded wire values (the enum spells them upper-case), so
#: the lookup in :func:`parse_loot_categories` is one fold and one get.
_CATEGORY_BY_VALUE: Final[Mapping[str, LootCategory]] = MappingProxyType(
    {category.value.casefold(): category for category in LootCategory}
)


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumericRange:
    """A declared, inclusive range for one numeric parameter."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise ValueError("a numeric range must not be empty")

    def check(self, value: float, *, name: str) -> None:
        """Raise unless *value* is inside the range."""
        if not self.minimum <= value <= self.maximum:
            raise ValueError(
                f"{name} must be within {self.minimum}..{self.maximum}, got {_render_value(value)}"
            )


#: Every numeric parameter the channel accepts, with the range it is checked
#: against. A parameter absent from this table cannot be declared in
#: :data:`GOAL_SPECS` — :func:`_check_tables` refuses the module at import.
NUMERIC_RANGES: Final[Mapping[str, NumericRange]] = MappingProxyType(
    {
        "target_level": NumericRange(1, MAX_SKILL_LEVEL),
        "satisfy_to": NumericRange(0.0, 1.0),
        "pages": NumericRange(1, MAX_READ_PAGES),
        "target_x": NumericRange(0, MAX_TARGET_SQUARE),
        "target_y": NumericRange(0, MAX_TARGET_SQUARE),
        "target_z": NumericRange(MIN_TARGET_FLOOR, MAX_TARGET_FLOOR),
        "radius": NumericRange(MIN_LOOT_RADIUS, MAX_LOOT_RADIUS),
        # The rest adapter's own bounds, imported rather than restated: a
        # target the channel admitted and the adapter refused would be a goal
        # that can only ever end in a downstream refusal.
        "target_endurance": NumericRange(MIN_REST_TARGET, MAX_REST_TARGET),
        # The floor is the sleep adapter's own; the ceiling is the channel's
        # narrower MAX_SLEEP_GOAL_HOURS, documented on that constant.
        "hours": NumericRange(MIN_SLEEP_HOURS, MAX_SLEEP_GOAL_HOURS),
    }
)

#: The parameters that are not numbers, restated so :func:`_check_tables` can
#: insist that everything else a spec declares has a row in
#: :data:`NUMERIC_RANGES`. Each entry here is validated by its own closed
#: check in :class:`GoalParams` instead: an enum member, a boolean, or the
#: closed-token string :func:`parse_loot_categories` owns.
_NON_NUMERIC_PARAMS: Final[frozenset[str]] = frozenset({"skill", "scope", "take_all", "categories"})


def _require_whole(value: object, *, name: str) -> int:
    """Narrow *value* to an ``int``, rejecting ``bool``.

    ``bool`` is an ``int`` subclass in Python and ``True`` would otherwise sail
    through a ``1..10`` range check as a level.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    return value


def _require_number(value: object, *, name: str) -> float:
    """Narrow *value* to a ``float``, rejecting ``bool`` and the unconvertible.

    An ``int`` is unbounded and a ``float`` is not, so ``float(10**400)`` raises
    ``OverflowError`` — which is not a ``ValueError``, so a caller catching the
    refusal this module documents would not catch it, and the channel would
    report a Python conversion failure instead of "that parameter is out of
    range". The size of the value is the refusal's own business, so it is caught
    here and answered in the module's own vocabulary.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    try:
        return float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a number of ordinary size") from None


def _require_line(detail: str, *, name: str) -> None:
    """Raise unless *detail* is one bounded line of printable text.

    The refusal does not quote *detail*: the branch this guards is the one a
    caller reaches by passing text it did not assemble itself, and that is
    exactly the text that must not be echoed into a traceback. A control
    character is refused as well as a long string, because a record's detail is
    read back into a single log line and a line break in it is a second line the
    caller chose the contents of.
    """
    if len(detail) > MAX_DETAIL_CHARS:
        raise ValueError(f"{name} must be at most {MAX_DETAIL_CHARS} characters")
    if any(character < " " or character == "\x7f" for character in detail):
        raise ValueError(f"{name} must be a single line of printable text")


@dataclass(frozen=True, slots=True)
class GoalParams:
    """The complete parameter surface of the channel.

    Every field is optional here and made required-or-forbidden by the kind in
    :class:`GoalRequest`; this class owns only the question "is this value in
    range and of the right type", which is worth asking on its own so that a
    :class:`GoalParams` can never hold an out-of-range number no matter which
    kind it is later attached to.
    """

    skill: TrainableSkill | None = None
    target_level: int | None = None
    satisfy_to: float | None = None
    pages: int | None = None
    #: Where a ``navigate_to`` goal walks to: the world square, floor included.
    #: Whole squares, like the movement adapter's own targets — a fractional
    #: coordinate aims between two cells and can only be refused downstream.
    target_x: int | None = None
    target_y: int | None = None
    target_z: int | None = None
    #: What "the area" means to a ``loot_area`` goal; the mission reads an
    #: absent scope as :attr:`LootScope.ROOM`, documented on the kind's spec.
    scope: LootScope | None = None
    #: The Chebyshev sweep of a ``scope=radius`` loot, in squares around the
    #: activation position. Meaningful only with that scope, and the pairing
    #: is enforced by :class:`GoalRequest`, which knows the kind.
    radius: int | None = None
    #: Widens a ``loot_area`` goal to every category, including the unknowns
    #: the ``useful_only`` default leaves on the shelf. Never overrides the
    #: user's reserves — that precedence belongs to the loot policy itself.
    take_all: bool | None = None
    #: A comma-joined list of :class:`~pz_agent_core.loot.LootCategory`
    #: tokens. Not free text: :func:`parse_loot_categories` validates every
    #: token against the closed enum at construction, so a value that reaches
    #: a mission is a set of reviewed tokens joined by commas and nothing
    #: else. A string field only because :class:`GoalParams` carries scalars.
    categories: str | None = None
    #: Where a ``rest_until`` goal stops resting: the endurance fraction the
    #: survival adapter is asked to reach and verifies from the observation.
    target_endurance: float | None = None
    #: How long a ``sleep_until_rested`` goal asks to sleep. Absent means the
    #: sleep adapter's own default — the mission omits the argument rather
    #: than restating the number here.
    hours: int | None = None

    def __post_init__(self) -> None:
        if self.skill is not None and not isinstance(self.skill, TrainableSkill):
            # Deliberately does not echo the value: this is the branch a raw
            # transcript reaches when someone routes around parse_skill.
            raise ValueError("skill must be a TrainableSkill member; use parse_skill first")
        if self.target_level is not None:
            level = _require_whole(self.target_level, name="target_level")
            NUMERIC_RANGES["target_level"].check(level, name="target_level")
        if self.satisfy_to is not None:
            fraction = _require_number(self.satisfy_to, name="satisfy_to")
            NUMERIC_RANGES["satisfy_to"].check(fraction, name="satisfy_to")
        if self.target_endurance is not None:
            target = _require_number(self.target_endurance, name="target_endurance")
            NUMERIC_RANGES["target_endurance"].check(target, name="target_endurance")
        if self.pages is not None:
            pages = _require_whole(self.pages, name="pages")
            NUMERIC_RANGES["pages"].check(pages, name="pages")
        for name in ("target_x", "target_y", "target_z", "radius", "hours"):
            value = getattr(self, name)
            if value is not None:
                coordinate = _require_whole(value, name=name)
                NUMERIC_RANGES[name].check(coordinate, name=name)
        if self.scope is not None and not isinstance(self.scope, LootScope):
            # Same shape as the skill check, for the same reason: the value a
            # caller routed around parse_scope with is not echoed.
            raise ValueError("scope must be a LootScope member; use parse_scope first")
        if self.take_all is not None and not isinstance(self.take_all, bool):
            raise ValueError("take_all must be true or false")
        if self.categories is not None:
            if not isinstance(self.categories, str):
                raise ValueError("categories must be a comma-joined string of category tokens")
            # Validated for effect, not parsed for storage: the field keeps the
            # caller's (checked) spelling and the mission re-parses it through
            # the same single door.
            parse_loot_categories(self.categories)

    def present(self) -> frozenset[str]:
        """Names of the parameters that were actually supplied."""
        return frozenset(f.name for f in fields(self) if getattr(self, f.name) is not None)

    def loot_categories(self) -> frozenset[LootCategory] | None:
        """The parsed ``categories`` selection, or None when none was supplied."""
        if self.categories is None:
            return None
        return parse_loot_categories(self.categories)


#: Declaration order of :class:`GoalParams`, restated so the spec table can be
#: checked against it at import rather than drifting silently.
PARAM_NAMES: Final[tuple[str, ...]] = (
    "skill",
    "target_level",
    "satisfy_to",
    "pages",
    "target_x",
    "target_y",
    "target_z",
    "scope",
    "radius",
    "take_all",
    "categories",
    "target_endurance",
    "hours",
)


# --------------------------------------------------------------------------
# budgets and specs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoalBudget:
    """The three bounds that together guarantee a terminal state.

    Wall clock alone is not enough: a goal whose steps all fail instantly would
    hold the channel for its full deadline doing nothing. Step count alone is
    not enough either: a single step that hangs never increments it. The pending
    time to live covers the third case, a goal admitted and never activated.
    """

    max_wall_ms: int
    max_steps: int
    pending_ttl_ms: int

    def __post_init__(self) -> None:
        if not MIN_GOAL_WALL_MS <= self.max_wall_ms <= MAX_GOAL_WALL_MS:
            raise ValueError(
                f"max_wall_ms must be within {MIN_GOAL_WALL_MS}..{MAX_GOAL_WALL_MS}, "
                f"got {_render_value(self.max_wall_ms)}"
            )
        if not 1 <= self.max_steps <= MAX_GOAL_STEPS:
            raise ValueError(
                f"max_steps must be within 1..{MAX_GOAL_STEPS}, got {_render_value(self.max_steps)}"
            )
        if not 1 <= self.pending_ttl_ms <= MAX_PENDING_TTL_MS:
            raise ValueError(
                f"pending_ttl_ms must be within 1..{MAX_PENDING_TTL_MS}, "
                f"got {_render_value(self.pending_ttl_ms)}"
            )


@dataclass(frozen=True, slots=True)
class GoalSpec:
    """Which parameters a kind requires, which it tolerates, and its budget."""

    required: frozenset[str]
    optional: frozenset[str]
    budget: GoalBudget

    def __post_init__(self) -> None:
        overlap = self.required & self.optional
        if overlap:
            raise ValueError(f"a parameter is either required or optional, not both: {overlap}")


_CONSUME_BUDGET: Final = GoalBudget(max_wall_ms=120_000, max_steps=4, pending_ttl_ms=60_000)
_READ_BUDGET: Final = GoalBudget(max_wall_ms=600_000, max_steps=4, pending_ttl_ms=120_000)

#: The navigation budget. ``max_steps`` is the goal channel's own ceiling and
#: bounds only the requests dispatched *through the goal seam* — final-leg
#: moves and their retries. The walking in between is bounded by the route
#: executor's own limits (``navigation.MAX_LEGS`` legs, ``MAX_REPLANS``
#: replans), which are deliberately not restated here: two copies of a bound
#: is how two subsystems come to disagree about it. The wall clock is the
#: umbrella over both.
_NAVIGATE_BUDGET: Final = GoalBudget(
    max_wall_ms=600_000, max_steps=MAX_GOAL_STEPS, pending_ttl_ms=120_000
)

#: The loot budget. Sized like :data:`_NAVIGATE_BUDGET` and for the same
#: reason: ``max_steps`` bounds only the requests dispatched *through the goal
#: seam* — for a loot mission that is the occasional completion probe, because
#: every approach leg, door, open, inspect and batch travels the loop's action
#: channel — while the mission's own bounds (candidates per mission,
#: consecutive failures, batches per container, each sub-action's adapter
#: budgets) bound the real work. The wall clock is the umbrella over all of it
#: and sits at the channel ceiling: a multi-container sweep is the longest
#: mission this channel carries, and the wire schema pins the ceiling at
#: fifteen minutes, so "longer" is a protocol change, not a bigger constant.
_LOOT_BUDGET: Final = GoalBudget(
    max_wall_ms=MAX_GOAL_WALL_MS, max_steps=MAX_GOAL_STEPS, pending_ttl_ms=120_000
)

#: The homeward budget. Navigate-sized, because ``return_home`` *is* one
#: journey — the only difference from ``navigate_to`` is where the target
#: comes from (the save's remembered home point instead of three submitted
#: coordinates), and a different budget for the same walk would be a claim
#: that walking home is a different amount of work than walking anywhere.
_RETURN_HOME_BUDGET: Final = _NAVIGATE_BUDGET

#: The explore budget. Sized like :data:`_LOOT_BUDGET` and for the same
#: reason: ``max_steps`` bounds only the goal-seam requests (completion
#: probes), every approach leg travels the loop's action channel, and the
#: mission's own bounds (waypoints per mission, consecutive failures, the
#: journey budgets under each approach) bound the real work. A frontier sweep
#: is mission-shaped like a loot sweep, and the wall clock is the umbrella.
_EXPLORE_BUDGET: Final = GoalBudget(
    max_wall_ms=MAX_GOAL_WALL_MS, max_steps=MAX_GOAL_STEPS, pending_ttl_ms=120_000
)

#: The treat budget. ``max_steps`` is sized to the care mission's own wound
#: ceiling (``MAX_WOUNDS_PER_MISSION`` = 8, pinned by a test rather than
#: imported — the channel keeps zero dependencies on the CLI): every bandage
#: and every transfer in front of one travels the loop's action channel, so
#: the goal seam only ever carries the no-work completion probe, and eight is
#: room for it several times over. Five minutes of wall clock covers eight
#: dressings at the bandage adapter's own thirty-second budget each.
_TREAT_BUDGET: Final = GoalBudget(max_wall_ms=300_000, max_steps=8, pending_ttl_ms=120_000)

#: The rest and sleep budgets, one constant because the two goals are the
#: same shape of work: one survival action whose adapter does the waiting
#: under its own wall-clock bounds, plus at most a completion probe on the
#: goal seam. The wall clock sits at the channel ceiling — endurance and
#: fatigue move at the game's pace, not this channel's — and the ceiling rule
#: from the loot wave applies: the wire schema pins fifteen minutes, so
#: "longer" is a protocol change, not a bigger constant here.
_REST_BUDGET: Final = GoalBudget(max_wall_ms=MAX_GOAL_WALL_MS, max_steps=4, pending_ttl_ms=120_000)
_SLEEP_BUDGET: Final = _REST_BUDGET

#: The whole channel in one table. Adding a kind without adding a row here
#: fails :func:`_check_tables` at import time, not at the first submission.
GOAL_SPECS: Final[Mapping[GoalKind, GoalSpec]] = MappingProxyType(
    {
        GoalKind.SATISFY_HUNGER: GoalSpec(
            required=frozenset(),
            optional=frozenset({"satisfy_to"}),
            budget=_CONSUME_BUDGET,
        ),
        GoalKind.SATISFY_THIRST: GoalSpec(
            required=frozenset(),
            optional=frozenset({"satisfy_to"}),
            budget=_CONSUME_BUDGET,
        ),
        GoalKind.READ_FOR_BOREDOM: GoalSpec(
            required=frozenset(),
            optional=frozenset({"pages"}),
            budget=_READ_BUDGET,
        ),
        GoalKind.TRAIN_SKILL: GoalSpec(
            required=frozenset({"skill"}),
            optional=frozenset({"target_level", "pages"}),
            budget=_READ_BUDGET,
        ),
        GoalKind.LEARN_RECIPE: GoalSpec(
            required=frozenset(),
            optional=frozenset({"pages"}),
            budget=_READ_BUDGET,
        ),
        GoalKind.NAVIGATE_TO: GoalSpec(
            required=frozenset({"target_x", "target_y", "target_z"}),
            optional=frozenset(),
            budget=_NAVIGATE_BUDGET,
        ),
        # Everything optional on purpose: the bare goal is the product's
        # founding sentence («облутай квартиру»), and it means scope=room,
        # useful_only selection, no take_all. The mission reads the absent
        # values as exactly those defaults.
        GoalKind.LOOT_AREA: GoalSpec(
            required=frozenset(),
            optional=frozenset({"scope", "radius", "take_all", "categories"}),
            budget=_LOOT_BUDGET,
        ),
        # A bare goal on purpose: «домой» carries everything it means. Where
        # home is comes from the save's memory (`pz-agent remember home`), and
        # a parameter here would be a second, spoken definition of home that
        # could disagree with the remembered one.
        GoalKind.RETURN_HOME: GoalSpec(
            required=frozenset(),
            optional=frozenset(),
            budget=_RETURN_HOME_BUDGET,
        ),
        # Both optional, and the absent scope means RADIUS — deliberately not
        # loot's ROOM default: the room the character stands in is the one
        # patch of world already observed, so "explore my own room" is a
        # no-op, while a bounded sweep around where they stand is the thing
        # the bare goal plausibly asks for. The mission reads the absence.
        GoalKind.EXPLORE_AREA: GoalSpec(
            required=frozenset(),
            optional=frozenset({"scope", "radius"}),
            budget=_EXPLORE_BUDGET,
        ),
        # Parameterless on purpose: «перевяжись» carries the whole goal. Which
        # wound and which dressing are policy.medical.select_treatment's
        # decisions, made deterministically per observation — a parameter here
        # would be a spoken second opinion on the triage order.
        GoalKind.TREAT_WOUNDS: GoalSpec(
            required=frozenset(),
            optional=frozenset(),
            budget=_TREAT_BUDGET,
        ),
        # The target is required because it is the goal: "rest" without a
        # stated endurance to reach has no postcondition to verify, and the
        # adapter's own default would be this channel choosing one silently.
        GoalKind.REST_UNTIL: GoalSpec(
            required=frozenset({"target_endurance"}),
            optional=frozenset(),
            budget=_REST_BUDGET,
        ),
        # ``hours`` optional: the absent value means the sleep adapter's own
        # default night, which the mission expresses by omitting the argument
        # rather than by restating the adapter's number.
        GoalKind.SLEEP_UNTIL_RESTED: GoalSpec(
            required=frozenset(),
            optional=frozenset({"hours"}),
            budget=_SLEEP_BUDGET,
        ),
    }
)

#: Convenience view of the same table, for callers that only want the bounds.
DEFAULT_BUDGETS: Final[Mapping[GoalKind, GoalBudget]] = MappingProxyType(
    {kind: spec.budget for kind, spec in GOAL_SPECS.items()}
)


def _check_tables() -> None:
    """Refuse to import a module whose tables disagree with its enums."""
    missing = set(GoalKind) - set(GOAL_SPECS)
    if missing:
        raise RuntimeError(f"GOAL_SPECS is missing a row for {sorted(k.value for k in missing)}")
    if set(PARAM_NAMES) != {f.name for f in fields(GoalParams)}:
        raise RuntimeError("PARAM_NAMES has drifted from GoalParams")
    for kind, spec in GOAL_SPECS.items():
        declared = spec.required | spec.optional
        unknown = declared - set(PARAM_NAMES)
        if unknown:
            raise RuntimeError(f"{kind.value} declares unknown parameter(s) {sorted(unknown)}")
        undeclared_numbers = declared - _NON_NUMERIC_PARAMS - set(NUMERIC_RANGES)
        if undeclared_numbers:
            raise RuntimeError(
                f"{kind.value} declares {sorted(undeclared_numbers)} with no range in "
                "NUMERIC_RANGES"
            )
    if _NON_NUMERIC_PARAMS & set(NUMERIC_RANGES):
        raise RuntimeError("a parameter is either numeric or closed-checked, never both")
    if _NON_NUMERIC_PARAMS - set(PARAM_NAMES):
        raise RuntimeError("_NON_NUMERIC_PARAMS names a parameter GoalParams does not carry")


_check_tables()


# --------------------------------------------------------------------------
# requests, records, refusals
# --------------------------------------------------------------------------


def key_digest(idempotency_key: str) -> str:
    """A short, stable, value-free fingerprint of an idempotency key.

    The channel indexes resubmissions by this rather than by the key itself, so
    the raw key exists only inside the caller's own frame. Sixteen hex
    characters over a queue that remembers a few dozen goals leaves a collision
    probability far below anything else that can go wrong here, and a collision
    produces a refusal rather than a wrong action.
    """
    return hashlib.blake2s(idempotency_key.encode("utf-8"), digest_size=8).hexdigest()


def mint_goal_id() -> str:
    """A fresh goal id.

    Minted here rather than accepted from the submitter for one reason: a goal
    id appears in refusals ("goal X is already active"), and an identifier that
    a caller chose is an identifier a caller could fill with text.
    """
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class GoalRequest:
    """One submission, validated against its kind before the queue sees it."""

    kind: GoalKind
    idempotency_key: str
    params: GoalParams = GoalParams()
    budget: GoalBudget | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, GoalKind):
            raise ValueError("kind must be a GoalKind member; use parse_kind first")
        if not _KEY_SHAPE.match(self.idempotency_key):
            # The key is not quoted, here or anywhere: this message reaches a
            # traceback, and the caller already knows what it sent.
            raise ValueError(
                "idempotency_key must be 1.."
                f"{MAX_IDEMPOTENCY_KEY_LEN} characters of letters, digits, "
                "'.', ':', '_' or '-', starting with a letter or digit"
            )
        spec = GOAL_SPECS[self.kind]
        present = self.params.present()
        missing = spec.required - present
        if missing:
            raise ValueError(f"{self.kind.value} requires {sorted(missing)}")
        extra = present - spec.required - spec.optional
        if extra:
            raise ValueError(f"{self.kind.value} takes no {sorted(extra)}")
        if (
            self.kind is GoalKind.LOOT_AREA
            and self.params.radius is not None
            and self.params.scope is not LootScope.RADIUS
        ):
            # A radius the mission would silently ignore is a caller believing
            # they bounded a sweep that is actually scoped to a room; the
            # mismatch is refused at the door, where it can still be fixed.
            raise ValueError("radius is meaningful only with scope=radius; set that scope too")
        if (
            self.kind is GoalKind.EXPLORE_AREA
            and self.params.radius is not None
            and self.params.scope not in (None, LootScope.RADIUS)
        ):
            # Same rule with explore's own default: the *absent* scope already
            # means radius here (the kind's spec says so), so a bare radius is
            # meaningful — only a radius beside room or building is the sweep
            # bound the mission would silently ignore.
            raise ValueError(
                "radius is meaningful only with scope=radius; drop the scope or set that one"
            )

    @property
    def effective_budget(self) -> GoalBudget:
        """The submitted budget, or the kind's declared default."""
        return self.budget if self.budget is not None else GOAL_SPECS[self.kind].budget

    @property
    def digest(self) -> str:
        return key_digest(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class GoalRecord:
    """A goal as the channel holds it. Immutable; the queue replaces it.

    The invariants in :meth:`__post_init__` are the goal-level restatement of
    the rule :class:`~pz_agent_core.protocol.messages.ActionResult` enforces for
    a single command: ``SUCCEEDED`` is a claim about the world and cannot be
    constructed without the evidence that was observed. The raw idempotency key
    is *not* a field — only :attr:`key_digest` is — so no repr of a goal, in a
    log line or a traceback, can carry it.
    """

    goal_id: str
    kind: GoalKind
    params: GoalParams
    budget: GoalBudget
    key_digest: str
    sequence: int
    state: GoalState
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    steps_used: int = 0
    reason_code: ReasonCode | None = None
    evidence_keys: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.submitted_at_ms < 0:
            raise ValueError("sequence and submitted_at_ms must be non-negative")
        if not self.goal_id:
            raise ValueError("a goal must carry the id this process minted for it")
        if not _DIGEST_SHAPE.match(self.key_digest):
            # Not "should have"; a record whose digest is missing or malformed is
            # a record no resubmission of its key can ever resolve to, so the
            # goal it stands for could be created a second time.
            raise ValueError("a goal must carry the digest of its idempotency key")
        _require_line(self.detail, name="detail")
        if not 0 <= self.steps_used <= self.budget.max_steps:
            raise ValueError(
                f"steps_used must be within 0..{self.budget.max_steps}, "
                f"got {_render_value(self.steps_used)}"
            )
        if self.state is GoalState.ACTIVE and self.started_at_ms is None:
            raise ValueError("an active goal must record when it started")
        # The channel reads a *wall* clock, which a time sync can step
        # backwards. These two checks are what turns that into a loud failure
        # instead of a goal that started after it finished.
        if self.started_at_ms is not None and self.started_at_ms < self.submitted_at_ms:
            raise ValueError("a goal cannot start before it was submitted")
        if (
            self.finished_at_ms is not None
            and self.started_at_ms is not None
            and self.finished_at_ms < self.started_at_ms
        ):
            raise ValueError("a goal cannot finish before it started")
        if self.finished_at_ms is not None and self.finished_at_ms < self.submitted_at_ms:
            raise ValueError("a goal cannot finish before it was submitted")
        if self.state.is_terminal:
            if self.reason_code is None:
                raise ValueError("a terminal goal must say why it ended")
            if self.finished_at_ms is None:
                raise ValueError("a terminal goal must record when it ended")
        elif self.reason_code is not None:
            raise ValueError("a goal that has not ended must not carry a reason code")
        succeeded = self.state is GoalState.SUCCEEDED
        if succeeded and self.reason_code is not ReasonCode.POSTCONDITION_MET:
            raise ValueError("a succeeded goal must carry POSTCONDITION_MET")
        if succeeded and not self.evidence_keys:
            raise ValueError("a succeeded goal requires observed postcondition evidence")
        if not succeeded and self.reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("only a succeeded goal may carry POSTCONDITION_MET")
        if len(self.evidence_keys) > MAX_EVIDENCE_KEYS:
            raise ValueError(f"at most {MAX_EVIDENCE_KEYS} evidence keys are kept")

    @property
    def is_open(self) -> bool:
        """True while the goal still occupies a slot in the channel."""
        return not self.state.is_terminal

    @property
    def deadline_ms(self) -> int | None:
        """When the active goal's wall clock runs out, or None while pending."""
        if self.started_at_ms is None:
            return None
        return self.started_at_ms + self.budget.max_wall_ms

    @property
    def pending_expiry_ms(self) -> int:
        """When an un-activated goal is given up on."""
        return self.submitted_at_ms + self.budget.pending_ttl_ms

    @property
    def steps_left(self) -> int:
        return self.budget.max_steps - self.steps_used


def normalise_evidence_keys(evidence: Mapping[str, object]) -> tuple[str, ...]:
    """The bounded, value-free record of what was observed.

    Keys only, sorted, capped, and any key that is not a protocol field name is
    recorded as :data:`_UNNAMED_EVIDENCE_KEY` — the mod forwards the game's
    strings, and game-authored text is untrusted data that must not end up in a
    goal's history.
    """
    kept: list[str] = []
    for name in sorted(evidence)[:MAX_EVIDENCE_KEYS]:
        kept.append(name if _EVIDENCE_KEY_SHAPE.match(name) else _UNNAMED_EVIDENCE_KEY)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class GoalRefusal:
    """Why a submission or an activation was not accepted.

    ``message`` is assembled from constants, numbers and identifiers this
    process minted. It never contains a caller-supplied string, a path or a
    payload value; :attr:`active_goal_id` is how "naming the active one" is
    satisfied without quoting anything the caller wrote.
    """

    reason_code: ReasonCode
    message: str
    active_goal_id: str = ""

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("a refusal must say what failed and what to do")
        if self.reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("POSTCONDITION_MET is not a refusal")


@dataclass(frozen=True, slots=True)
class GoalTransition:
    """One observed change of state, reported by the call that caused it."""

    goal_id: str
    kind: GoalKind
    previous: GoalState
    state: GoalState
    reason_code: ReasonCode | None
    at_ms: int
    detail: str = ""

    def __post_init__(self) -> None:
        if self.previous is self.state:
            raise ValueError("a transition must change the state")
        if self.state.is_terminal and self.reason_code is None:
            raise ValueError("a terminal transition must say why")
        _require_line(self.detail, name="detail")


@dataclass(frozen=True, slots=True)
class GoalAdmission:
    """The answer to "may this goal in", with the reason when it is no.

    Returned by both :meth:`~.queue.GoalQueue.submit` and
    :meth:`~.queue.GoalQueue.activate_next`.
    ``duplicate`` distinguishes "admitted" from "you already submitted this" —
    both hand back a record, and only one of them created anything.
    """

    goal: GoalRecord | None = None
    refusal: GoalRefusal | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if (self.goal is None) == (self.refusal is None):
            raise ValueError("an admission carries exactly one of a goal or a refusal")
        if self.duplicate and self.goal is None:
            raise ValueError("a refusal cannot be a duplicate admission")

    @property
    def accepted(self) -> bool:
        return self.goal is not None


# --------------------------------------------------------------------------
# the bridge to the deterministic planner
# --------------------------------------------------------------------------

#: Total by construction — :func:`_check_planner_mapping` refuses the import
#: otherwise, so a new :class:`GoalKind` cannot reach the queue before the
#: planner knows how to serve it.
_PLANNER_KIND: Final[Mapping[GoalKind, PlannerGoalKind]] = MappingProxyType(
    {
        GoalKind.SATISFY_HUNGER: PlannerGoalKind.SATISFY_HUNGER,
        GoalKind.SATISFY_THIRST: PlannerGoalKind.SATISFY_THIRST,
        GoalKind.READ_FOR_BOREDOM: PlannerGoalKind.READ_FOR_BOREDOM,
        GoalKind.TRAIN_SKILL: PlannerGoalKind.TRAIN_SKILL,
        GoalKind.LEARN_RECIPE: PlannerGoalKind.LEARN_RECIPE,
        # Served by the deterministic route executor, never by a plan provider
        # — NullProvider refuses the kind by name — but the mapping stays total
        # because the goal still crosses the planner seam (the loop widens the
        # record with to_planner_goal before the navigating planner sees it).
        GoalKind.NAVIGATE_TO: PlannerGoalKind.NAVIGATE_TO,
        # Same arrangement one epic later: the CLI's deterministic loot
        # mission serves it behind the same wrapper, and NullProvider refuses
        # it by name for a loop assembled without that wrapper.
        GoalKind.LOOT_AREA: PlannerGoalKind.LOOT_AREA,
        # And the wave after that, twice over: one journey to the remembered
        # home point, and one frontier-driven explore mission — both served
        # behind the same wrapper, both refused by name by NullProvider.
        GoalKind.RETURN_HOME: PlannerGoalKind.RETURN_HOME,
        GoalKind.EXPLORE_AREA: PlannerGoalKind.EXPLORE_AREA,
        # The care wave, three at once: bandaging, resting and sleeping are
        # driven by the CLI's deterministic care missions over the medical and
        # survival adapters, and NullProvider refuses each by name for a loop
        # assembled without that wrapper.
        GoalKind.TREAT_WOUNDS: PlannerGoalKind.TREAT_WOUNDS,
        GoalKind.REST_UNTIL: PlannerGoalKind.REST_UNTIL,
        GoalKind.SLEEP_UNTIL_RESTED: PlannerGoalKind.SLEEP_UNTIL_RESTED,
    }
)


def _check_planner_mapping() -> None:
    missing = set(GoalKind) - set(_PLANNER_KIND)
    if missing:
        raise RuntimeError(
            f"no planner goal for {sorted(k.value for k in missing)}; a goal the planner "
            "cannot serve must not be admissible"
        )


_check_planner_mapping()


def to_planner_goal(record: GoalRecord) -> PlannerGoal:
    """Widen a channel goal into the planner's own :class:`Goal`.

    The skill crosses back from the enum to the ``str`` the planner takes. That
    is the correct direction of travel: narrowing happens at the edge, once, and
    everything downstream of it is working with a value that was checked.
    """
    skill = record.params.skill.value if record.params.skill is not None else ""
    return PlannerGoal(goal_id=record.goal_id, kind=_PLANNER_KIND[record.kind], skill=skill)
