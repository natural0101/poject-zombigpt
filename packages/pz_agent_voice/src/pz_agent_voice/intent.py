"""Deterministic classification of a transcript into a closed intent.

No model runs here, and none may: the stop word has to be recognised in the
time it takes to compare a few strings, from any state, whatever the planner is
doing. A classifier that could be slow, wrong or unavailable would put the
single most important behaviour in this package behind the least reliable part
of the system.

Two rules shape the module:

* :func:`is_stop` is answered on its own, from the raw word tokens, before any
  other question is asked. It does not consult the wake state, the session, the
  planner or the confidence score.
* Everything else resolves to a member of :class:`~.messages.VoiceGoal` or to
  nothing. When two goals match, the answer is *ambiguous*, not the first one in
  some arbitrary order — guessing between "поесть" and "попить" is exactly the
  behaviour the blueprint asks to be replaced with a question.

:func:`resolve_goal` is the same discipline applied one level up, against the
typed goal channel rather than against the session's own small vocabulary. It
answers with a :class:`~pz_agent_core.goals.GoalKind` and a range-checked
:class:`~pz_agent_core.goals.GoalParams`, or with a named
:class:`~.messages.IntentRefusal` — never with anything in between, and never
with a kind assembled out of what the recogniser heard. The rule that makes that
last part true is structural rather than careful: the only values that can leave
this module are enum members declared in the core, integers parsed from a closed
numeral table, and the names of parameters and capabilities this process itself
minted. There is no branch here that copies a token into a field.

The grammar and the kind set are checked against each other at import
(:func:`check_grammar`). A :class:`~pz_agent_core.goals.GoalKind` with no Russian
phrasing would be unreachable by speech and a phrasing naming a kind that no
longer exists would be a matcher pointed at nothing; both are import failures
rather than something discovered at the microphone.
"""

from __future__ import annotations

# Several of the words below are short enough that every letter in them has a
# Latin lookalike, which reads to the confusable-character rule as mistyped
# ASCII. They are not mistyped: they are the vocabulary this matcher exists to
# recognise, and taking the rule's suggestion would stop each of them matching.
# ruff: noqa: RUF001
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from pz_agent_core.capabilities import DRINK_CARRIED, EAT_PERCENTAGE, READ_LITERATURE
from pz_agent_core.goals import (
    GOAL_SPECS,
    NUMERIC_RANGES,
    PARAM_NAMES,
    GoalKind,
    GoalParams,
    TrainableSkill,
)

from .messages import IntentRefusal, VoiceGoal, VoiceInput, VoiceIntent

__all__ = [
    "AFFIRM_WORDS",
    "ALL_VOICE_CAPABILITIES",
    "CAPABILITY_FOR_KIND",
    "DEFAULT_WAKE_WORDS",
    "DENY_WORDS",
    "GOAL_WORDS",
    "KIND_WORDS",
    "MAX_NUMBER_CHARS",
    "NUMBER_WORDS",
    "READING_KINDS",
    "SKILL_WORDS",
    "STATUS_WORDS",
    "STOP_WORDS",
    "UNIT_WINDOW",
    "UNIT_WORDS",
    "GoalResolution",
    "IntentMatch",
    "check_grammar",
    "classify",
    "extract_quantities",
    "is_stop",
    "matched_goals",
    "matched_kinds",
    "matched_skill",
    "matched_skills",
    "resolve_goal",
]

#: Words that mean "stop now", in both languages the blueprint's examples use.
#: Deliberately narrow: every member must be unambiguous on its own, because a
#: word in here fires from any state and cancels whatever is running. "Отмена"
#: is *not* here — it is how a user declines a clarification question, and a
#: declined question must not disarm the session.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "стоп",
        "стой",
        "стойте",
        "стоять",
        "остановись",
        "остановите",
        "остановить",
        "хватит",
        "прекрати",
        "прекратить",
        "stop",
        "halt",
        "abort",
    }
)

DEFAULT_WAKE_WORDS: Final[frozenset[str]] = frozenset({"агент", "ассистент", "agent"})

GOAL_WORDS: Final[dict[VoiceGoal, frozenset[str]]] = {
    VoiceGoal.EAT: frozenset(
        {"поешь", "поесть", "съешь", "съесть", "покушай", "еда", "еду", "eat", "food"}
    ),
    VoiceGoal.DRINK: frozenset(
        {"попей", "попить", "выпей", "выпить", "пить", "вода", "воды", "drink", "water"}
    ),
    VoiceGoal.READ: frozenset(
        {"почитай", "почитать", "читай", "читать", "книга", "книгу", "read", "book"}
    ),
    VoiceGoal.RESUME: frozenset({"продолжай", "продолжи", "продолжить", "resume", "continue"}),
}

STATUS_WORDS: Final[frozenset[str]] = frozenset({"статус", "состояние", "status", "report"})

AFFIRM_WORDS: Final[frozenset[str]] = frozenset({"да", "ага", "давай", "yes", "ok", "окей"})

#: "Нет" and "отмена" decline; "не" alone is far too common in ordinary speech
#: to read as a refusal, so it is not a member.
DENY_WORDS: Final[frozenset[str]] = frozenset({"нет", "отмена", "отставить", "no", "cancel"})


@dataclass(frozen=True, slots=True)
class IntentMatch:
    """What a transcript resolved to.

    ``goals`` carries every goal the words matched, so the caller can tell "the
    user said one thing" from "the user said two things and one of them has to
    be picked". ``goal`` is set only in the unambiguous case.
    """

    intent: VoiceIntent
    goals: tuple[VoiceGoal, ...] = ()
    woke: bool = False

    @property
    def goal(self) -> VoiceGoal | None:
        return self.goals[0] if self.intent is VoiceIntent.GOAL else None


def is_stop(raw: VoiceInput) -> bool:
    """True when *raw* contains a stop word.

    Answered from the word tokens of an interim transcript just as readily as a
    final one: waiting for the recogniser to settle before honouring "стоп"
    would add the recogniser's endpointing delay to the one latency in this
    system that has to stay short.
    """
    return not STOP_WORDS.isdisjoint(raw.words())


def matched_goals(words: tuple[str, ...]) -> tuple[VoiceGoal, ...]:
    """Every goal whose vocabulary *words* touches, in declaration order."""
    seen = set(words)
    return tuple(goal for goal, vocabulary in GOAL_WORDS.items() if not vocabulary.isdisjoint(seen))


def classify(raw: VoiceInput, *, wake_words: frozenset[str] = DEFAULT_WAKE_WORDS) -> IntentMatch:
    """Resolve *raw* to one intent.

    Callers must have answered :func:`is_stop` first; this function reports a
    stop transcript as :attr:`~.messages.VoiceIntent.STOP` for completeness, but
    the stop *path* must not run through the rest of this classification, which
    is why the two entry points are separate.
    """
    words = raw.words()
    if not STOP_WORDS.isdisjoint(words):
        return IntentMatch(intent=VoiceIntent.STOP)

    woke = not wake_words.isdisjoint(words)
    goals = matched_goals(words)
    if len(goals) > 1:
        return IntentMatch(intent=VoiceIntent.AMBIGUOUS, goals=goals, woke=woke)
    if len(goals) == 1:
        return IntentMatch(intent=VoiceIntent.GOAL, goals=goals, woke=woke)
    if not STATUS_WORDS.isdisjoint(words):
        return IntentMatch(intent=VoiceIntent.STATUS, woke=woke)
    if not AFFIRM_WORDS.isdisjoint(words):
        return IntentMatch(intent=VoiceIntent.AFFIRM, woke=woke)
    if not DENY_WORDS.isdisjoint(words):
        return IntentMatch(intent=VoiceIntent.DENY, woke=woke)
    if woke:
        return IntentMatch(intent=VoiceIntent.WAKE, woke=True)
    return IntentMatch(intent=VoiceIntent.UNKNOWN)


# --------------------------------------------------------------------------
# the typed goal channel: vocabulary
# --------------------------------------------------------------------------

#: Longest run of digits treated as a number. Anything longer is reported as
#: :data:`_ABOVE_EVERY_RANGE` rather than parsed: the exact value is not needed,
#: because every range in :data:`~pz_agent_core.goals.NUMERIC_RANGES` tops out
#: three orders of magnitude below it, and refusing to build an arbitrarily
#: large integer out of a transcript is the point of having a bound at all.
MAX_NUMBER_CHARS: Final = 6

#: A number's meaning comes from the unit word that follows it, and Russian puts
#: that word within a token or two ("до пятого уровня", "на восемьдесят
#: процентов"). Looking further would let a number in one clause capture a unit
#: from the next one.
UNIT_WINDOW: Final = 2

_ABOVE_EVERY_RANGE: Final = 10**MAX_NUMBER_CHARS

_DIGITS: Final = re.compile(r"^[0-9]+$")

#: The Russian phrasings for each kind. Built on top of :data:`GOAL_WORDS` where
#: the two overlap, so the session's own vocabulary and the goal channel's
#: cannot drift into disagreeing about what "поешь" means.
#:
#: ``TRAIN_SKILL`` and ``LEARN_RECIPE`` share the reading action with
#: ``READ_FOR_BOREDOM``; the words here are only the *generic* ones for each, and
#: :func:`_settle_reading` decides between them.
KIND_WORDS: Final[dict[GoalKind, frozenset[str]]] = {
    GoalKind.SATISFY_HUNGER: GOAL_WORDS[VoiceGoal.EAT]
    | frozenset({"перекуси", "перекусить", "покушать", "голоден", "голодный", "проголодался"}),
    GoalKind.SATISFY_THIRST: GOAL_WORDS[VoiceGoal.DRINK]
    | frozenset({"напейся", "напиться", "жажда", "пьет", "попейте"}),
    GoalKind.READ_FOR_BOREDOM: GOAL_WORDS[VoiceGoal.READ]
    | frozenset({"почитайка", "журнал", "скучно", "скука"}),
    GoalKind.TRAIN_SKILL: frozenset(
        {
            "прокачай",
            "прокачать",
            "прокачивай",
            "тренируй",
            "тренируйся",
            "тренировать",
            "изучай",
            "изучить",
            "навык",
            "навыки",
            "train",
            "skill",
        }
    ),
    GoalKind.LEARN_RECIPE: frozenset(
        {"рецепт", "рецепты", "рецептов", "рецепту", "выучи", "выучить", "recipe"}
    ),
}

#: The three kinds served by the same in-game action. Their overlap is resolved
#: by specificity rather than by declaration order — see :func:`_settle_reading`.
READING_KINDS: Final[frozenset[GoalKind]] = frozenset(
    {GoalKind.READ_FOR_BOREDOM, GoalKind.TRAIN_SKILL, GoalKind.LEARN_RECIPE}
)

#: What a user calls each skill. Single tokens only: a bigram matcher would have
#: to decide what "первая" means on its own, and there is no honest answer.
SKILL_WORDS: Final[dict[TrainableSkill, frozenset[str]]] = {
    TrainableSkill.CARPENTRY: frozenset(
        {"плотник", "плотника", "плотницкое", "плотницкому", "столярку", "столярка"}
    ),
    TrainableSkill.COOKING: frozenset({"готовку", "готовка", "кулинарию", "кулинария", "повара"}),
    TrainableSkill.FARMING: frozenset(
        {"фермерство", "фермерству", "огород", "огородничество", "грядки"}
    ),
    TrainableSkill.ELECTRICAL: frozenset({"электрику", "электрика", "электричество", "проводку"}),
    TrainableSkill.METALWORKING: frozenset({"металл", "металлообработку", "сварку", "сварка"}),
    TrainableSkill.MECHANICS: frozenset({"механику", "механика", "машины", "автомеханику"}),
    TrainableSkill.TAILORING: frozenset({"шитье", "портняжное", "швейное", "ткачество"}),
    TrainableSkill.FORAGING: frozenset({"собирательство", "собирательству", "травы"}),
    TrainableSkill.FISHING: frozenset({"рыбалку", "рыбалка", "рыбную"}),
    TrainableSkill.TRAPPING: frozenset({"ловушки", "капканы", "силки"}),
    TrainableSkill.FIRST_AID: frozenset({"медицину", "медицина", "медицине", "перевязку"}),
}

#: The unit word that gives a bare number its parameter. Keyed by the parameter
#: name so :func:`check_grammar` can hold this table against
#: :data:`~pz_agent_core.goals.NUMERIC_RANGES`.
UNIT_WORDS: Final[dict[str, frozenset[str]]] = {
    "target_level": frozenset({"уровня", "уровень", "уровню", "уровне", "лвл", "level"}),
    "pages": frozenset({"страниц", "страницы", "страницу", "страница", "pages", "page"}),
    "satisfy_to": frozenset({"процентов", "процента", "процент", "процентах", "percent"}),
}

#: ``satisfy_to`` is a fraction in the core and a percentage in speech. Nobody
#: says "насыть меня до нуля целых восьми"; the conversion lives here, once, so
#: the range check downstream still sees the core's own units.
_SPOKEN_AS_PERCENT: Final[frozenset[str]] = frozenset({"satisfy_to"})

#: Numerals a recogniser actually emits for these quantities. Deliberately
#: single-token: "восемьдесят пять" is two tokens and is not recognised, which is
#: why digits are accepted alongside — a recogniser configured for numerals emits
#: "85" for exactly the cases this table cannot reach.
NUMBER_WORDS: Final[dict[str, int]] = {
    "один": 1,
    "одну": 1,
    "первого": 1,
    "два": 2,
    "две": 2,
    "второго": 2,
    "три": 3,
    "третьего": 3,
    "четыре": 4,
    "четвертого": 4,
    "пять": 5,
    "пятого": 5,
    "шесть": 6,
    "шестого": 6,
    "семь": 7,
    "седьмого": 7,
    "восемь": 8,
    "восьмого": 8,
    "девять": 9,
    "девятого": 9,
    "десять": 10,
    "десятого": 10,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
}

#: The capability each kind needs on the installed build. Not a guess about the
#: game: these are the names :mod:`pz_agent_core.capabilities.probes` publishes,
#: so "is this usable here" is answered by the same report the MCP tool gate
#: reads, and a refusal can name the thing the user would have to fix.
CAPABILITY_FOR_KIND: Final[dict[GoalKind, str]] = {
    GoalKind.SATISFY_HUNGER: EAT_PERCENTAGE,
    GoalKind.SATISFY_THIRST: DRINK_CARRIED,
    GoalKind.READ_FOR_BOREDOM: READ_LITERATURE,
    GoalKind.TRAIN_SKILL: READ_LITERATURE,
    GoalKind.LEARN_RECIPE: READ_LITERATURE,
}

ALL_VOICE_CAPABILITIES: Final[frozenset[str]] = frozenset(CAPABILITY_FOR_KIND.values())

#: Which refusal to speak when a kind's required parameter was not heard. Keyed
#: by parameter name so that a kind that starts requiring something new fails
#: :func:`_check_channel_tables` instead of silently producing a goal without it.
_MISSING_PARAM_REFUSAL: Final[dict[str, IntentRefusal]] = {
    "skill": IntentRefusal.SKILL_NOT_NAMED,
}

_UNIT_OWNER: Final[dict[str, str]] = {
    word: name for name, vocabulary in UNIT_WORDS.items() for word in vocabulary
}


def check_grammar(vocabularies: Mapping[str, frozenset[str]]) -> None:
    """Raise unless *vocabularies* names exactly the goal kinds that exist.

    Takes the kind as a ``str`` rather than a :class:`GoalKind` on purpose. The
    failure this guards against is a grammar written against a kind that was
    renamed or removed, and a mapping already keyed by the enum cannot express
    that failure — it would not import. Keyed by the wire value, it can, and
    :func:`check_grammar` is then a real check rather than a restatement of the
    type.
    """
    declared = set(vocabularies)
    known = {kind.value for kind in GoalKind}
    unknown = declared - known
    if unknown:
        raise RuntimeError(
            f"the intent grammar names goal kind(s) that do not exist: {sorted(unknown)}"
        )
    missing = known - declared
    if missing:
        raise RuntimeError(f"no Russian phrasing for goal kind(s) {sorted(missing)}")
    empty = sorted(name for name, words in vocabularies.items() if not words)
    if empty:
        raise RuntimeError(f"goal kind(s) {empty} have an empty vocabulary and cannot be reached")
    seen: dict[str, str] = {}
    for name, words in vocabularies.items():
        for word in sorted(words):
            owner = seen.setdefault(word, name)
            if owner != name:
                raise RuntimeError(f"{word!r} is claimed by both {owner} and {name}")


def _check_channel_tables() -> None:
    """Refuse to import a grammar that has drifted from the core's own tables."""
    check_grammar({kind.value: words for kind, words in KIND_WORDS.items()})
    if set(UNIT_WORDS) != set(NUMERIC_RANGES):
        raise RuntimeError("UNIT_WORDS and NUMERIC_RANGES describe different parameters")
    if set(UNIT_WORDS) | {"skill"} != set(PARAM_NAMES):
        raise RuntimeError("a goal parameter has no unit word and cannot be spoken")
    if set(_SPOKEN_AS_PERCENT) - set(NUMERIC_RANGES):
        raise RuntimeError("a percentage parameter has no declared range")
    if set(CAPABILITY_FOR_KIND) != set(GoalKind):
        raise RuntimeError("every goal kind must name the capability it needs")
    required = {name for spec in GOAL_SPECS.values() for name in spec.required}
    unspoken = required - set(_MISSING_PARAM_REFUSAL)
    if unspoken:
        raise RuntimeError(f"no refusal names the missing parameter(s) {sorted(unspoken)}")
    duplicated = sorted(
        word
        for word in _UNIT_OWNER
        if sum(word in vocabulary for vocabulary in UNIT_WORDS.values()) > 1
    )
    if duplicated:
        raise RuntimeError(f"unit word(s) {duplicated} name more than one parameter")
    claimed: dict[str, TrainableSkill] = {}
    for skill, words in SKILL_WORDS.items():
        for word in sorted(words):
            owner = claimed.setdefault(word, skill)
            if owner is not skill:
                raise RuntimeError(f"{word!r} is claimed by both {owner.value} and {skill.value}")


_check_channel_tables()


# --------------------------------------------------------------------------
# the typed goal channel: matching
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoalResolution:
    """What a transcript resolved to on the typed goal channel.

    Exactly one of three things, enforced in the constructor: a stop, a goal
    with typed parameters, or a named refusal. There is no fourth state and in
    particular no "goal, probably" — a resolution that carried both a kind and a
    doubt would be read as a kind by the first caller that forgot to check.

    ``parameter`` and ``capability`` are names this process minted, checked
    against the core's own tables here so that the spoken form built from them
    cannot be handed something that came off a microphone.
    """

    intent: VoiceIntent
    kind: GoalKind | None = None
    params: GoalParams = field(default_factory=GoalParams)
    refusal: IntentRefusal | None = None
    parameter: str = ""
    capability: str = ""
    candidates: tuple[GoalKind, ...] = ()

    def __post_init__(self) -> None:
        if self.intent is VoiceIntent.STOP:
            if self.kind is not None or self.refusal is not None:
                raise ValueError("a stop is neither a goal nor a refusal")
        elif self.intent is VoiceIntent.GOAL:
            if self.kind is None or self.refusal is not None:
                raise ValueError("a resolved goal carries a kind and no refusal")
        elif self.refusal is None or self.kind is not None:
            raise ValueError("anything that is not a stop or a goal must name a refusal")
        if self.parameter and self.parameter not in PARAM_NAMES:
            raise ValueError("parameter must name a declared goal parameter")
        if self.capability and self.capability not in ALL_VOICE_CAPABILITIES:
            raise ValueError("capability must be one this package knows how to require")
        if self.refusal is IntentRefusal.CAPABILITY_UNAVAILABLE and not self.capability:
            raise ValueError("an unavailable capability must be named")
        if (
            self.refusal
            in (IntentRefusal.PARAMETER_OUT_OF_RANGE, IntentRefusal.PARAMETER_NOT_ACCEPTED)
            and not self.parameter
        ):
            raise ValueError("a rejected parameter must be named")
        if self.refusal is IntentRefusal.AMBIGUOUS_GOAL and len(self.candidates) < 2:
            raise ValueError("an ambiguous resolution must name the alternatives")

    @property
    def resolved(self) -> bool:
        """True when a goal came out of this and can be submitted."""
        return self.intent is VoiceIntent.GOAL


def matched_kinds(words: tuple[str, ...]) -> tuple[GoalKind, ...]:
    """Every goal kind whose vocabulary *words* touches, in declaration order."""
    seen = set(words)
    return tuple(kind for kind, vocabulary in KIND_WORDS.items() if not vocabulary.isdisjoint(seen))


def matched_skills(words: tuple[str, ...]) -> tuple[TrainableSkill, ...]:
    """Every skill *words* names, in declaration order."""
    seen = set(words)
    return tuple(
        skill for skill, vocabulary in SKILL_WORDS.items() if not vocabulary.isdisjoint(seen)
    )


def matched_skill(words: tuple[str, ...]) -> TrainableSkill | None:
    """The one skill *words* names, or None when it names none or several.

    Several is folded into none deliberately. "Прокачай плотника и механику" has
    no single answer, and picking the earlier member of an enum would be the
    matcher choosing for the user in the one place a question costs nothing.
    """
    found = matched_skills(words)
    return found[0] if len(found) == 1 else None


def _as_number(token: str) -> int | None:
    """The number *token* spells, or None when it spells no number."""
    value = NUMBER_WORDS.get(token)
    if value is not None:
        return value
    if not _DIGITS.match(token):
        return None
    if len(token) > MAX_NUMBER_CHARS:
        return _ABOVE_EVERY_RANGE
    return int(token)


def extract_quantities(words: tuple[str, ...]) -> dict[str, int]:
    """Every number in *words* that a unit word gave a meaning to, as spoken.

    Values are the quantity the user said, in the unit they said it in — the
    conversion into the core's own units happens once, in :func:`resolve_goal`,
    where the range check can see the result. A number with no unit word after
    it is dropped: it names no parameter, and deciding which one it meant would
    be the invention this module exists to avoid.
    """
    found: dict[str, int] = {}
    for position, token in enumerate(words):
        value = _as_number(token)
        if value is None:
            continue
        for unit in words[position + 1 : position + 1 + UNIT_WINDOW]:
            name = _UNIT_OWNER.get(unit)
            if name is not None:
                found.setdefault(name, value)
                break
    return found


def _settle_reading(words: tuple[str, ...], reading: tuple[GoalKind, ...]) -> GoalKind:
    """Pick the most specific of the three kinds served by reading a book.

    Specificity, not declaration order: "почитай про рецепты" is a request for
    a recipe and "почитай плотницкое" is a request to train, and both of them
    also match the generic ``READ_FOR_BOREDOM`` vocabulary. Treating that as an
    ambiguity would ask the user to choose between a thing they said and a
    weaker version of the same thing.
    """
    if GoalKind.LEARN_RECIPE in reading:
        return GoalKind.LEARN_RECIPE
    if GoalKind.TRAIN_SKILL in reading or matched_skills(words):
        return GoalKind.TRAIN_SKILL
    return GoalKind.READ_FOR_BOREDOM


def resolve_goal(raw: VoiceInput, *, available: frozenset[str]) -> GoalResolution:
    """Resolve *raw* to a goal kind with typed parameters, or refuse by name.

    *available* is the set of capability names the installed build was observed
    to support. It has no default: defaulting it would mean this function
    assumed every capability worked, which is precisely the claim AGENTS.md's
    capability-honesty rule forbids making without evidence.

    The order of the checks is the specification:

    1. **Stop, before anything else.** A transcript containing a stop word is a
       stop even when it also contains a goal word, a quantity and a skill. The
       check is first so that no amount of grammar below it can consume the
       sentence first and hand back a goal.
    2. One kind, or a refusal naming the alternatives.
    3. The capability, before the parameters: validating a quantity for
       something the build cannot do would refuse the wrong thing.
    4. The parameters, each against the kind's own spec and its declared range.
    """
    words = raw.words()
    if not STOP_WORDS.isdisjoint(words):
        return GoalResolution(intent=VoiceIntent.STOP)

    kinds = matched_kinds(words)
    reading = tuple(kind for kind in kinds if kind in READING_KINDS)
    if reading:
        settled = _settle_reading(words, reading)
        kinds = (*(kind for kind in kinds if kind not in READING_KINDS), settled)
    if not kinds:
        return _refuse(IntentRefusal.NOT_A_GOAL)
    if len(kinds) > 1:
        return _refuse(IntentRefusal.AMBIGUOUS_GOAL, candidates=kinds)

    kind = kinds[0]
    capability = CAPABILITY_FOR_KIND[kind]
    if capability not in available:
        return _refuse(IntentRefusal.CAPABILITY_UNAVAILABLE, capability=capability)

    spec = GOAL_SPECS[kind]
    accepted = spec.required | spec.optional
    skill = matched_skill(words)
    quantities = extract_quantities(words)

    if skill is not None and "skill" not in accepted:
        return _refuse(IntentRefusal.PARAMETER_NOT_ACCEPTED, parameter="skill")

    typed: dict[str, float] = {}
    # Iterated in the core's declaration order rather than in the order the
    # words arrived, so two bad parameters in one sentence always produce the
    # same refusal.
    for name in PARAM_NAMES:
        spoken = quantities.get(name)
        if spoken is None:
            continue
        if name not in accepted:
            return _refuse(IntentRefusal.PARAMETER_NOT_ACCEPTED, parameter=name)
        value = spoken / 100 if name in _SPOKEN_AS_PERCENT else float(spoken)
        try:
            NUMERIC_RANGES[name].check(value, name=name)
        except ValueError:
            # The range's own message quotes the number back. That is right for
            # a traceback and wrong for a loudspeaker, so only the fact of the
            # failure survives; the sentence is built from the closed table in
            # phrases.py instead.
            return _refuse(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter=name)
        typed[name] = value

    present = set(typed) | ({"skill"} if skill is not None else set())
    for name in PARAM_NAMES:
        if name in spec.required and name not in present:
            return _refuse(_MISSING_PARAM_REFUSAL[name], parameter=name)

    params = GoalParams(
        skill=skill,
        target_level=int(typed["target_level"]) if "target_level" in typed else None,
        satisfy_to=typed.get("satisfy_to"),
        pages=int(typed["pages"]) if "pages" in typed else None,
    )
    return GoalResolution(intent=VoiceIntent.GOAL, kind=kind, params=params)


def _refuse(
    refusal: IntentRefusal,
    *,
    parameter: str = "",
    capability: str = "",
    candidates: tuple[GoalKind, ...] = (),
) -> GoalResolution:
    """Build the one shape a declined transcript may take."""
    return GoalResolution(
        intent=VoiceIntent.UNKNOWN,
        refusal=refusal,
        parameter=parameter,
        capability=capability,
        candidates=candidates,
    )
