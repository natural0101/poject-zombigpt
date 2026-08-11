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

from pz_agent_core.capabilities import (
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)
from pz_agent_core.capabilities.probes import MEDICAL_BANDAGE
from pz_agent_core.goals import (
    GOAL_SPECS,
    NUMERIC_RANGES,
    PARAM_NAMES,
    GoalKind,
    GoalParams,
    TrainableSkill,
)

from .messages import IntentRefusal, VoiceGoal, VoiceInput, VoiceIntent, normalise_transcript

__all__ = [
    "AFFIRM_WORDS",
    "ALL_VOICE_CAPABILITIES",
    "BARE_NUMBER_PARAM",
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
    "UNSPEAKABLE_KINDS",
    "UNSPEAKABLE_PARAMS",
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

#: Everything that is not a word character separates tokens, restated here for
#: the import-time vocabulary check: a table entry that tokenisation would split
#: is an entry no transcript can ever match, and it has to fail the import
#: rather than sit in the grammar looking correct.
_NON_WORD: Final = re.compile(r"[^\w]+", re.UNICODE)

#: The percent sign survives no tokenisation — :meth:`~.messages.VoiceInput.
#: words` drops it — so it is looked for in the normalised text instead. It
#: means exactly what "процентов" means, and it binds exactly a digit run: a
#: recogniser configured for numerals emits "80%", and a recogniser emitting
#: words says the unit word. ASCII digits only, for the same reason
#: :data:`_DIGITS` is ASCII-only.
_PERCENT: Final = re.compile(r"([0-9]+)\s*%")

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
    # The first parameterless kind added since the voice epic, speakable for
    # the same reason satisfy_hunger is: the bare word carries the whole
    # goal. «домой» / «идём домой» / «возвращайся домой» all reach the same
    # member through these tokens; where home *is* stays in the save's
    # memory, so nothing spoken here ever becomes a coordinate.
    GoalKind.RETURN_HOME: frozenset(
        {
            "домой",
            "дом",
            "дому",
            "возвращайся",
            "вернись",
            "вернуться",
            "возвращаться",
            "home",
        }
    ),
    # Parameterless like return_home, and speakable on the same argument: the
    # bare word carries the whole goal, and everything after it — which wound,
    # which dressing — is the medical policy's decision, never the
    # transcript's. «перевязку» is deliberately absent: it is first aid's
    # *skill* word, and the one-namespace rule below would rightly refuse a
    # token two tables claim.
    GoalKind.TREAT_WOUNDS: frozenset(
        {
            "перевяжись",
            "перевяжи",
            "перевязать",
            "забинтуй",
            "забинтуйся",
            "бинтуй",
            "обработай",
            "раны",
            "рану",
            "bandage",
            "wounds",
        }
    ),
    # Parameterless and urgent, speakable on return_home's argument taken to
    # its sharpest case: the bare word carries the whole goal, and the person
    # shouting it is watching a zombie close in. Where to retreat *to* is the
    # avoid mission's deterministic decision from the observed threat picture
    # — nothing spoken here ever becomes a coordinate or a direction. «стой»
    # and friends stay stop words: stopping and retreating are different
    # orders, and the one-namespace rule keeps them from ever blurring.
    GoalKind.AVOID_THREAT: frozenset(
        {
            "отступай",
            "отступи",
            "отступить",
            "отступаем",
            "беги",
            "бегите",
            "убегай",
            "уходи",
            "уходим",
            "спрячься",
            "укройся",
            "retreat",
            "flee",
        }
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
        {
            "плотник",
            "плотника",
            "плотницкое",
            "плотницкому",
            "плотничество",
            "плотничеству",
            "столярку",
            "столярка",
        }
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

#: Kinds deliberately absent from the spoken grammar. ``navigate_to`` requires
#: an exact world square, and three coordinates dictated over a microphone are
#: a guess wearing numbers: one misheard digit walks the character hundreds of
#: squares from where the speaker stood watching, silently, which is precisely
#: the kind of invention this module refuses everywhere else. The kind travels
#: through ``pz_goal_submit``, where the caller types the square.
#:
#: ``loot_area`` sits here for a narrower reason, and the honest one: the bare
#: goal is a legitimate spoken sentence («облутай квартиру» is the product's
#: founding phrase, and it needs no parameter at all), but every parameter the
#: kind *optionally* takes — the scope token, the radius, ``take_all``, the
#: category list — is unspeakable in this grammar, whose parameter machinery
#: speaks numbers-with-unit-words and skills and nothing else. The partition
#: check below refuses a speakable kind that takes unspeakable parameters even
#: optionally, on purpose: a kind speech can start but never qualify is a
#: grammar that answers «облутай только еду» by looting everything. Giving the
#: sentence a vocabulary therefore means first giving the grammar closed-token
#: parameters, which is the voice epic's change, not the loot epic's; until
#: then the kind travels through ``pz_goal_submit`` like navigation does.
#: ``explore_area`` sits here by loot_area's own argument, one wave later:
#: the bare goal is speakable in principle, but its optional parameters — the
#: scope token and the radius — are the very tokens this grammar declares
#: unspeakable, and the partition check refuses a speakable kind that takes
#: unspeakable parameters even optionally. A spoken «исследуй только двор»
#: that swept the default radius instead would be the invention that check
#: exists to prevent; the kind travels through ``pz_goal_submit`` until the
#: grammar grows closed-token parameters.
#:
#: ``return_home`` is deliberately *not* here: it takes no parameter at all,
#: so it is the first kind since the voice epic to clear the partition check
#: as speakable, and :data:`KIND_WORDS` carries its vocabulary.
#:
#: ``rest_until`` sits here by the pinned required-parameter argument:
#: ``target_endurance`` is a fraction with no unit word of its own — the
#: percent vocabulary already belongs to ``satisfy_to``, and one namespace
#: across the tables is the rule — so the kind's *required* parameter is
#: unspeakable and the kind travels through ``pz_goal_submit``.
#:
#: ``sleep_until_rested`` sits here by loot_area's argument, decided honestly
#: against the grammar's own checks rather than inherited: the bare goal is a
#: legitimate spoken sentence («выспись» needs no parameter at all), and its
#: one optional parameter ``hours`` even has the number-with-unit-word shape
#: this grammar's machinery speaks — but the spoken-quantity path runs through
#: more than this module's unit table (the session's spoken-scale table
#: converts every speakable numeric parameter, and its import check refuses a
#: speakable number it cannot convert), and this wave does not grow that path.
#: Until the grammar carries an hour quantity end to end, a speakable sleep
#: would be a kind speech can start but never qualify — «поспи два часа» that
#: silently slept the default night is the invention the partition check
#: exists to prevent — so the kind travels through ``pz_goal_submit``.
#:
#: ``treat_wounds`` is deliberately *not* here: parameterless like
#: ``return_home``, it clears the partition check as speakable and
#: :data:`KIND_WORDS` carries its vocabulary.
#:
#: ``engage_single_zombie`` is here for a reason unlike every other entry,
#: and it is not a parameter-machinery gap to be closed by a later grammar:
#: it is the partition doing the exact job it exists for. The kind is
#: parameterless and would clear the mechanical check as speakable — and it
#: must never be speakable anyway, because it is a kill order. A misheard
#: transcript resolving to any other kind wastes a sandwich or a walk; a
#: misheard transcript resolving to this one swings a weapon at whatever the
#: mission observes nearest, on the authority of words nobody said. The
#: retreat kind is speakable precisely because its worst mishearing makes
#: the character *safer*; combat's worst mishearing is the single most
#: harmful action this project can take, so the kind travels only through
#: ``pz_goal_submit``, where the caller types the kind. Deliberate, and
#: pinned by its own test so relaxing it is a reviewed decision.
#:
#: ``craft_item`` is the second entry of that second sort, and the decision
#: was taken here rather than inherited. The kind's *required* parameter is
#: ``product``, an item type spelled the way the build files it
#: (``Base.SpearCrude``), and a transcript cannot be trusted to spell one: a
#: recogniser hears «копьё», not a type, and the two are joined by a table
#: nothing in this process is allowed to hold — what a build can craft is the
#: build's fact, and a closed product vocabulary written here would be a
#: second, drifting copy of the game's (``pz_agent_core.policy.crafting``
#: refuses to keep one for the same reason). A *small* closed set was the
#: alternative and it was rejected on its own merits, not on effort: the set
#: would be a guess at which recipes this install has, correct only for the
#: vanilla ones, and a spoken «сделай копьё» that resolved to the one spear
#: this module happened to know would spend the wrong planks with the user's
#: sentence as its authority. That is the same shape as the kill order above,
#: with one difference that makes it no better — the mishearing cannot be
#: walked back. A misheard retreat makes the character safer, a misheard
#: sandwich costs a sandwich, and a misheard craft destroys materials no
#: later observation can return. So the kind travels only through
#: ``pz_goal_submit``, where the caller *types* the product, and the grammar
#: says nothing rather than guessing. Pinned by its own test.
#:
#: ``build_structure`` is the craft's argument again, and it does not need to
#: borrow any of it: this kind fails the mechanical check twice over before the
#: judgement is even reached. Its ``structure`` is a blueprint identifier, as
#: unspellable by a recogniser as a product is, and its square is three
#: coordinates — already the tokens ``navigate_to`` is unspeakable for. So the
#: partition would refuse it whatever anyone thought.
#:
#: What is worth writing down is why nobody should try to close that gap later.
#: The craft's case ends "a misheard craft destroys materials no later
#: observation can return". Here the sentence is stronger and should be read as
#: stronger: a misheard *product* wastes planks, and a misheard *square* puts a
#: permanent wall somewhere nobody asked for — in a doorway, across the only
#: stair, around the character — and **this project ships nothing that takes one
#: down**. There is no demolition action, by design: removing what somebody put
#: there is a different authority and this build does not have it. So the
#: mistake has no undo at all, not even an expensive one, and the input that
#: could make it is a stream of words a room's noise can bend. The coordinates
#: make it worse rather than better: a digit misheard in «постройся на 1200
#: 3400» is not a wrong word, it is a different square, and the sentence still
#: parses. Every action this kind issues is P4 besides, which has no autonomous
#: path in this codebase — so the kind travels only through ``pz_goal_submit``,
#: where the caller types the blueprint and types the square and can read both
#: back before granting the placement. Pinned by its own test.
UNSPEAKABLE_KINDS: Final[frozenset[GoalKind]] = frozenset(
    {
        GoalKind.NAVIGATE_TO,
        GoalKind.LOOT_AREA,
        GoalKind.EXPLORE_AREA,
        GoalKind.REST_UNTIL,
        GoalKind.SLEEP_UNTIL_RESTED,
        GoalKind.ENGAGE_SINGLE_ZOMBIE,
        GoalKind.CRAFT_ITEM,
        GoalKind.BUILD_STRUCTURE,
    }
)

#: The parameters only those kinds carry, excluded from the unit-word table
#: for the same reason the kinds are excluded from the phrasing table. Held by
#: :func:`_check_channel_tables` against :data:`~pz_agent_core.goals.GOAL_SPECS`
#: so no speakable kind can ever require a parameter speech cannot supply.
UNSPEAKABLE_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "target_x",
        "target_y",
        "target_z",
        "scope",
        "radius",
        "take_all",
        "categories",
        # rest_until's required target: a fraction whose natural unit word
        # ("процентов") is already satisfy_to's, and a word two parameters
        # claim is a number the grammar could not honestly assign.
        "target_endurance",
        # sleep_until_rested's optional night length: unit-word-shaped, and
        # still unspeakable until the whole spoken-quantity path carries an
        # hour scale — the reasoning is on UNSPEAKABLE_KINDS above.
        "hours",
        # craft_item's required product: an item type as the build spells it,
        # which is the one goal parameter no closed vocabulary in this process
        # may hold. There is nothing for a matcher to match against, and the
        # full argument is on UNSPEAKABLE_KINDS above.
        "product",
        # craft_item's optional run count. Unit-word-shaped like `hours` and
        # unspeakable for the same reason plus a sharper one: even with an
        # hour scale to borrow, a number the grammar bound to the wrong
        # parameter here would authorise runs that destroy materials, and the
        # kind that carries it is unspeakable anyway.
        "count",
        # build_structure's required blueprint: a game identifier like the
        # product above, and unspeakable for that reason plus the one that
        # cannot be engineered away — a misheard product wastes materials, a
        # misheard blueprint (or the coordinates beside it, which are already
        # unspeakable as navigate_to's) raises something permanent that nothing
        # in this build can take back down. The full argument is on
        # UNSPEAKABLE_KINDS above.
        "structure",
    }
)

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

#: The one kind for which a bare number has an unambiguous meaning: "прокачай
#: механику до 7" is a target level and can be nothing else. Every other kind
#: leaves an unbound number unassigned — deciding what it meant would be the
#: invention this module exists to avoid. A closed table rather than a branch,
#: so :func:`_check_channel_tables` can hold each entry against the kind's own
#: spec at import.
BARE_NUMBER_PARAM: Final[dict[GoalKind, str]] = {GoalKind.TRAIN_SKILL: "target_level"}

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
    # Walking home is the movement capability and nothing else: the target
    # comes from memory, and the journey's doors and stairs ride the same
    # move the probe verified.
    GoalKind.RETURN_HOME: MOVE_TO_SQUARE,
    # Bandaging needs exactly the adapter that serves it; rest_until and
    # sleep_until_rested are unspeakable and so — per the check below —
    # deliberately absent.
    GoalKind.TREAT_WOUNDS: MEDICAL_BANDAGE,
    # Retreating is walking away: the movement capability, exactly as
    # return_home. The threat picture comes from the observation, which
    # every build that observes at all provides.
    GoalKind.AVOID_THREAT: MOVE_TO_SQUARE,
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
    unspeakable = {kind.value for kind in UNSPEAKABLE_KINDS}
    unknown = declared - known
    if unknown:
        raise RuntimeError(
            f"the intent grammar names goal kind(s) that do not exist: {sorted(unknown)}"
        )
    spoken_anyway = declared & unspeakable
    if spoken_anyway:
        # Both tables claiming a kind is the drift this check exists for: a
        # phrasing for a kind the grammar refuses on purpose is one of the two
        # declarations being wrong, and neither may win silently.
        raise RuntimeError(
            f"goal kind(s) {sorted(spoken_anyway)} are declared unspeakable and phrased anyway"
        )
    missing = known - declared - unspeakable
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
    """Refuse to import a grammar that has drifted from the core's own tables.

    Every failure below is silent at runtime if it is allowed through: a
    vocabulary word that does not survive normalisation simply never matches, a
    skill with no vocabulary is unreachable by speech, and a stop word that is
    also a goal word is a safety defect that would only show up when somebody
    shouted it.
    """
    check_grammar({kind.value: words for kind, words in KIND_WORDS.items()})
    missing_skills = set(TrainableSkill) - set(SKILL_WORDS)
    if missing_skills:
        raise RuntimeError(
            f"no vocabulary for skill(s) {sorted(skill.value for skill in missing_skills)}"
        )
    # One namespace across every table a goal-channel matcher reads, the stop
    # vocabulary included: a word two tables claim answers two questions at
    # once, and a vocabulary word that is also a stop word could never be
    # anything but a stop. Checked against the normalised form as well, because
    # a table entry that tokenisation would rewrite or split is an entry no
    # transcript can ever match.
    groups: list[tuple[str, frozenset[str]]] = [
        *((kind.value, words) for kind, words in KIND_WORDS.items()),
        *((skill.value, words) for skill, words in SKILL_WORDS.items()),
        *((f"unit:{name}", words) for name, words in UNIT_WORDS.items()),
        ("numbers", frozenset(NUMBER_WORDS)),
        ("stop", STOP_WORDS),
    ]
    seen: dict[str, str] = {}
    for owner, words in groups:
        for word in sorted(words):
            if normalise_transcript(word) != word or _NON_WORD.search(word):
                raise RuntimeError(f"{owner} declares {word!r}, which no transcript can match")
            first = seen.setdefault(word, owner)
            if first != owner:
                raise RuntimeError(f"{word!r} is claimed by both {first} and {owner}")
    if set(UNIT_WORDS) & UNSPEAKABLE_PARAMS:
        raise RuntimeError("a parameter declared unspeakable has a unit word anyway")
    if set(UNIT_WORDS) - set(NUMERIC_RANGES):
        raise RuntimeError("a unit word names a parameter with no declared numeric range")
    # Not an equality any more: UNSPEAKABLE_PARAMS also carries the loot
    # goal's closed-token parameters, which have no numeric range to appear
    # in. What must stay true is that every *numeric* parameter is reachable
    # by exactly one route — a unit word or the unspeakable list.
    if set(NUMERIC_RANGES) - (set(UNIT_WORDS) | UNSPEAKABLE_PARAMS):
        raise RuntimeError("a numeric parameter has no unit word and is not declared unspeakable")
    if set(UNIT_WORDS) | {"skill"} | UNSPEAKABLE_PARAMS != set(PARAM_NAMES):
        raise RuntimeError("a goal parameter has no unit word and cannot be spoken")
    for kind, spec in GOAL_SPECS.items():
        if kind in UNSPEAKABLE_KINDS:
            continue
        leaked = (spec.required | spec.optional) & UNSPEAKABLE_PARAMS
        if leaked:
            # The partition must stay clean both ways: a speakable kind taking
            # an unspeakable parameter would admit goals speech can start and
            # never finish.
            raise RuntimeError(
                f"{kind.value} is speakable but takes unspeakable parameter(s) {sorted(leaked)}"
            )
    if set(_SPOKEN_AS_PERCENT) - set(NUMERIC_RANGES):
        raise RuntimeError("a percentage parameter has no declared range")
    for kind, parameter in BARE_NUMBER_PARAM.items():
        spec = GOAL_SPECS[kind]
        if parameter not in NUMERIC_RANGES:
            raise RuntimeError(f"{parameter} has no declared range and cannot be a bare number")
        if parameter not in spec.required | spec.optional:
            raise RuntimeError(f"{kind.value} does not accept {parameter} as a bare number")
    if set(CAPABILITY_FOR_KIND) & UNSPEAKABLE_KINDS:
        raise RuntimeError("a kind declared unspeakable names a capability anyway")
    if set(CAPABILITY_FOR_KIND) | UNSPEAKABLE_KINDS != set(GoalKind):
        raise RuntimeError("every speakable goal kind must name the capability it needs")
    required = {
        name
        for kind, spec in GOAL_SPECS.items()
        if kind not in UNSPEAKABLE_KINDS
        for name in spec.required
    }
    unspoken = required - set(_MISSING_PARAM_REFUSAL)
    if unspoken:
        raise RuntimeError(f"no refusal names the missing parameter(s) {sorted(unspoken)}")


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


def _percent_quantity(text: str) -> int | None:
    """The number the percent sign binds in *text*, or None when it binds none.

    The sign is "процентов" for a recogniser that emits numerals, and it glues
    to the digits it follows, so it is read from the normalised text rather than
    from the word tokens that dropped it. The digit run is bounded exactly as
    :func:`_as_number` bounds one: a run past :data:`MAX_NUMBER_CHARS` is
    reported as above every range rather than parsed.
    """
    match = _PERCENT.search(text)
    if match is None:
        return None
    return _as_number(match.group(1))


def _bare_number(words: tuple[str, ...]) -> int | None:
    """The one number *words* contain, or None when they contain none or several.

    Several is folded into none deliberately, exactly as :func:`matched_skill`
    folds two skills: with two numbers and no unit words there is no honest
    answer to "which one did the kind's bare-number parameter mean".
    """
    found = [value for value in (_as_number(token) for token in words) if value is not None]
    return found[0] if len(found) == 1 else None


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
       A number reaches one through a unit word, through the percent sign —
       which is the unit word "процентов" as a numeral-emitting recogniser
       spells it — or, for the one kind :data:`BARE_NUMBER_PARAM` declares a
       meaning for, on its own. Every route ends at the same range check.
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
    percent = _percent_quantity(raw.normalised())
    if percent is not None:
        # The sign means what the unit word means; a sentence carrying both
        # keeps the word's binding, which arrived through the same window every
        # other quantity uses.
        quantities.setdefault("satisfy_to", percent)
    bare = BARE_NUMBER_PARAM.get(kind)
    if bare is not None and not quantities:
        # Only when no unit bound anything: a unit word in the sentence means
        # the speaker names their quantities, and a leftover number next to a
        # named one is not a second parameter.
        spoken_alone = _bare_number(words)
        if spoken_alone is not None:
            quantities[bare] = spoken_alone

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

    try:
        params = GoalParams(
            skill=skill,
            target_level=int(typed["target_level"]) if "target_level" in typed else None,
            satisfy_to=typed.get("satisfy_to"),
            pages=int(typed["pages"]) if "pages" in typed else None,
        )
    except ValueError:
        # Only reachable if the ranges this module checked against stop
        # agreeing with the ones the core enforces in GoalParams. The exception
        # text quotes the number the user spoke, which is right for a traceback
        # and wrong for everything a refusal reaches, so only the fact of the
        # failure survives.
        return _refuse(IntentRefusal.INTERNAL)
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
