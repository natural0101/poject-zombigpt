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
    "MAX_PARSED_TOKEN_CHARS",
    "MAX_PENDING_TTL_MS",
    "MAX_RENDERED_VALUE_CHARS",
    "MAX_SKILL_LEVEL",
    "MAX_TARGET_FLOOR",
    "MAX_TARGET_SQUARE",
    "MIN_GOAL_WALL_MS",
    "MIN_TARGET_FLOOR",
    "NUMERIC_RANGES",
    "PARAM_NAMES",
    "TERMINAL_GOAL_STATES",
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
    "NumericRange",
    "TrainableSkill",
    "key_digest",
    "mint_goal_id",
    "normalise_evidence_keys",
    "parse_kind",
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

    ``NAVIGATE_TO`` is the one kind not served by a plan provider: the sidecar's
    deterministic route executor (``pz_agent_core.navigation``) walks it square
    by square, and :class:`~pz_agent_core.planner.provider.NullProvider` refuses
    it by name rather than approximating it with a plan. The mapping to the
    planner vocabulary stays total all the same, because the goal still crosses
    the planner seam on its way to the executor.
    """

    SATISFY_HUNGER = "satisfy_hunger"
    SATISFY_THIRST = "satisfy_thirst"
    READ_FOR_BOREDOM = "read_for_boredom"
    TRAIN_SKILL = "train_skill"
    LEARN_RECIPE = "learn_recipe"
    NAVIGATE_TO = "navigate_to"


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


_KIND_BY_VALUE: Final[Mapping[str, GoalKind]] = MappingProxyType({k.value: k for k in GoalKind})
_SKILL_BY_VALUE: Final[Mapping[str, TrainableSkill]] = MappingProxyType(
    {s.value: s for s in TrainableSkill}
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
    }
)


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
        if self.pages is not None:
            pages = _require_whole(self.pages, name="pages")
            NUMERIC_RANGES["pages"].check(pages, name="pages")
        for name in ("target_x", "target_y", "target_z"):
            value = getattr(self, name)
            if value is not None:
                coordinate = _require_whole(value, name=name)
                NUMERIC_RANGES[name].check(coordinate, name=name)

    def present(self) -> frozenset[str]:
        """Names of the parameters that were actually supplied."""
        return frozenset(f.name for f in fields(self) if getattr(self, f.name) is not None)


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
        undeclared_numbers = declared - {"skill"} - set(NUMERIC_RANGES)
        if undeclared_numbers:
            raise RuntimeError(
                f"{kind.value} declares {sorted(undeclared_numbers)} with no range in "
                "NUMERIC_RANGES"
            )


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
