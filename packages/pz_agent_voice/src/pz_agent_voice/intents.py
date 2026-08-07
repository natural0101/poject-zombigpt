"""Russian utterances onto the typed goal channel, or onto an explicit refusal.

This module is the only place in the voice package that names a
:class:`~pz_agent_core.goals.GoalKind`, and it is deliberately the narrowest
thing that can be: :func:`resolve` takes a string that a microphone produced and
returns a :class:`VoiceResolution` built entirely out of enum members, numbers
this process derived, and sentences that are module constants. **No byte of the
utterance is carried in the return value, and there is no branch that can put
one there** — see :class:`VoiceRefusal`, which has no message field at all, only
a code that indexes :data:`REFUSAL_MESSAGES`.

Four rules shape the resolution order, and each is checked by a test rather than
described:

* **Stop wins, and wins first.** The stop vocabulary is scanned before the
  length bound, before the empty check and before any kind is considered, and it
  is :data:`~.intent.STOP_WORDS` itself rather than a second copy — two stop
  lists in one process is one stop list that silently stops agreeing with the
  other. A stop heard as a goal is the failure this ordering exists to prevent.
* **Closed, or refused.** :data:`KIND_WORDS` is the complete map from speech to
  a kind. A phrase that touches no row resolves to
  :attr:`VoiceRefusalCode.UNMAPPED`; a phrase that touches two resolves to
  :attr:`VoiceRefusalCode.AMBIGUOUS_KIND`. Nothing here guesses, and nothing
  here mints a kind that :data:`~pz_agent_core.goals.GOAL_SPECS` has no row for.
* **Typed and in range, or refused.** A number reaches a goal only through a
  named unit ("страниц", "процентов", "уровня") or through the one kind that
  declares a bare-number meaning, and only after it has been checked against the
  channel's own :data:`~pz_agent_core.goals.NUMERIC_RANGES`. Out of range is
  :attr:`VoiceRefusalCode.OUT_OF_RANGE` — which does *not* say what the number
  was, because the number came from the utterance.
* **Bounded before matching.** :data:`MAX_UTTERANCE_CHARS` is applied to the raw
  string before it is normalised, so everything after it is linear in a bounded
  string; nothing is accumulated between calls. An utterance longer than the
  bound cannot become a goal at all — matching a kind out of the surviving
  prefix would be reading half a sentence — but it is still scanned for a stop
  word first, so the safety path survives the bound.

  The bound's honest limit: a stop word occurring *after* the first
  :data:`MAX_UTTERANCE_CHARS` characters of one utterance is not seen here. The
  mitigation is upstream and is where it belongs — the driver runs
  :func:`~.intent.is_stop` against every interim transcript, so the stop is
  caught while the sentence is still short.
"""

from __future__ import annotations

# The vocabularies below are short Cyrillic words whose every letter has a Latin
# lookalike, which reads to the confusable-character rule as mistyped ASCII.
# They are not mistyped: they are what this matcher exists to recognise, and
# taking the rule's suggestion would stop each of them matching anything.
# ruff: noqa: RUF001
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pz_agent_core.goals import (
    GOAL_SPECS,
    MAX_IDEMPOTENCY_KEY_LEN,
    NUMERIC_RANGES,
    PARAM_NAMES,
    GoalKind,
    GoalParams,
    GoalRefusal,
    GoalRequest,
    NumericRange,
    TrainableSkill,
)
from pz_agent_core.protocol import ReasonCode

from .intent import STOP_WORDS
from .messages import MAX_TEXT_CHARS, MAX_TRANSCRIPT_CHARS, normalise_transcript

__all__ = [
    "BARE_NUMBER_PARAM",
    "KIND_WORDS",
    "MAX_NUMBER_DIGITS",
    "MAX_UTTERANCE_CHARS",
    "MAX_VOICE_SEQUENCE",
    "REFUSAL_MESSAGES",
    "REFUSAL_REASONS",
    "SKILL_WORDS",
    "UNIT_WORDS",
    "VoiceOutcome",
    "VoiceRefusal",
    "VoiceRefusalCode",
    "VoiceResolution",
    "bounded_words",
    "is_stop_utterance",
    "matched_kinds",
    "matched_skills",
    "mint_idempotency_key",
    "resolve",
    "to_goal_request",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Longest utterance this module will look at, applied to the raw string before
#: anything else touches it. A spoken command is a clause; recognisers that emit
#: an accumulated paragraph are emitting context, not an instruction, and the
#: right answer to a paragraph is to ask for a shorter one.
MAX_UTTERANCE_CHARS: Final = 160

#: Longest run of digits read as a number. Three digits covers every range in
#: :data:`~pz_agent_core.goals.NUMERIC_RANGES` with room to spare, so a longer
#: run is refused as out of range rather than converted — ``int()`` on an
#: arbitrarily long digit string is unbounded work for a value that cannot be
#: accepted anyway.
MAX_NUMBER_DIGITS: Final = 3

#: Ceiling on the counter :func:`mint_idempotency_key` formats. Keys are minted
#: from a kind and a number and from nothing else, so no transcript byte can
#: reach the channel through the one caller-supplied string it accepts.
MAX_VOICE_SEQUENCE: Final = 999_999

_KEY_PREFIX: Final = "voice"

#: Everything that is not a word character separates tokens. Token equality,
#: never substring search: a matcher that fires on substrings reads "стопка" as
#: "стоп" and — far worse, because a false stop is harmless — reads the wrong
#: half of a compound word as a goal.
_NON_WORD: Final = re.compile(r"[^\w]+", re.UNICODE)

#: The percent sign survives no tokenisation, so it is looked for in the
#: normalised text. It means exactly what "процентов" means.
_PERCENT_SIGN: Final = "%"


# --------------------------------------------------------------------------
# outcomes and refusals
# --------------------------------------------------------------------------


class VoiceOutcome(StrEnum):
    """What an utterance turned into. Exhaustive: there is no fourth answer."""

    STOP = "stop"
    GOAL = "goal"
    REFUSED = "refused"


class VoiceRefusalCode(StrEnum):
    """Why an utterance did not become a goal.

    A code, not a sentence, because the sentence is a constant looked up from
    :data:`REFUSAL_MESSAGES`: a refusal that formatted anything from the
    utterance into its message would put a transcript into a traceback, a log
    line and a support bundle in one step.
    """

    EMPTY = "empty"
    TOO_LONG = "too_long"
    UNMAPPED = "unmapped"
    AMBIGUOUS_KIND = "ambiguous_kind"
    UNKNOWN_SKILL = "unknown_skill"
    AMBIGUOUS_SKILL = "ambiguous_skill"
    CONFLICTING_PARAMETERS = "conflicting_parameters"
    AMBIGUOUS_NUMBER = "ambiguous_number"
    MISSING_NUMBER = "missing_number"
    UNITLESS_NUMBER = "unitless_number"
    PARAMETER_NOT_ALLOWED = "parameter_not_allowed"
    OUT_OF_RANGE = "out_of_range"
    INTERNAL = "internal"


#: One sentence per code, each naming what failed and what to say instead, none
#: containing a slot. Short enough to be spoken through
#: :class:`~.messages.VoiceOutput` without truncation, which
#: :func:`_check_tables` verifies against :data:`~.messages.MAX_TEXT_CHARS`.
REFUSAL_MESSAGES: Final[Mapping[VoiceRefusalCode, str]] = MappingProxyType(
    {
        VoiceRefusalCode.EMPTY: "Не расслышал. Повтори команду.",
        VoiceRefusalCode.TOO_LONG: "Слишком длинная фраза. Скажи короче, одной командой.",
        VoiceRefusalCode.UNMAPPED: (
            "Не знаю такой команды. Скажи: поесть, попить, почитать, "
            "прокачать навык или выучить рецепт."
        ),
        VoiceRefusalCode.AMBIGUOUS_KIND: "Услышал две команды сразу. Назови одну.",
        VoiceRefusalCode.UNKNOWN_SKILL: (
            "Не понял, какой навык. Назови навык, например: плотничество, готовка, механика."
        ),
        VoiceRefusalCode.AMBIGUOUS_SKILL: "Услышал два навыка. Назови один.",
        VoiceRefusalCode.CONFLICTING_PARAMETERS: "Услышал две разные величины. Назови одну.",
        VoiceRefusalCode.AMBIGUOUS_NUMBER: "Услышал два числа. Назови одно.",
        VoiceRefusalCode.MISSING_NUMBER: "Не услышал число. Повтори команду с числом.",
        VoiceRefusalCode.UNITLESS_NUMBER: (
            "Не понял, к чему число. Скажи: страниц, процентов или уровня."
        ),
        VoiceRefusalCode.PARAMETER_NOT_ALLOWED: (
            "Эта величина к такой команде не подходит. Повтори команду без нее."
        ),
        VoiceRefusalCode.OUT_OF_RANGE: "Число вне допустимого диапазона. Назови другое.",
        VoiceRefusalCode.INTERNAL: "Не смог разобрать команду. Повтори ее.",
    }
)

#: How each refusal is reported to anything that speaks the core's wire codes.
#: All but one are argument problems: the utterance was understood as far as the
#: grammar goes and rejected on its content.
REFUSAL_REASONS: Final[Mapping[VoiceRefusalCode, ReasonCode]] = MappingProxyType(
    {
        VoiceRefusalCode.EMPTY: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.TOO_LONG: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.UNMAPPED: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.AMBIGUOUS_KIND: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.UNKNOWN_SKILL: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.AMBIGUOUS_SKILL: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.CONFLICTING_PARAMETERS: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.AMBIGUOUS_NUMBER: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.MISSING_NUMBER: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.UNITLESS_NUMBER: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.PARAMETER_NOT_ALLOWED: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.OUT_OF_RANGE: ReasonCode.INVALID_ARGUMENT,
        VoiceRefusalCode.INTERNAL: ReasonCode.INTERNAL_ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class VoiceRefusal:
    """Why an utterance was not admitted, in a form that cannot quote it.

    ``parameter`` is a member of :data:`~pz_agent_core.goals.PARAM_NAMES` — an
    identifier this repository chose, not a word anybody said — and ``allowed``
    is the range the channel declares for it. Together they let a caller say
    "the level has to be 1 to 10" without repeating the level it heard.
    """

    code: VoiceRefusalCode
    parameter: str = ""
    allowed: NumericRange | None = None

    def __post_init__(self) -> None:
        if self.parameter and self.parameter not in PARAM_NAMES:
            raise ValueError("parameter must name a declared goal parameter")
        if self.allowed is not None and not self.parameter:
            raise ValueError("a declared range must say which parameter it bounds")

    @property
    def message(self) -> str:
        """The sentence for this code. Always a module constant."""
        return REFUSAL_MESSAGES[self.code]

    @property
    def reason_code(self) -> ReasonCode:
        return REFUSAL_REASONS[self.code]

    def to_goal_refusal(self) -> GoalRefusal:
        """This refusal in the core's own shape, for anything that reports one."""
        return GoalRefusal(reason_code=self.reason_code, message=self.message)


@dataclass(frozen=True, slots=True)
class VoiceResolution:
    """The whole answer: an outcome, and the one thing that outcome carries.

    ``truncated`` records that :data:`MAX_UTTERANCE_CHARS` actually bit. It is a
    boolean rather than a length so that the resolution says the bound was hit
    without saying how much was said.
    """

    outcome: VoiceOutcome
    kind: GoalKind | None = None
    params: GoalParams = field(default_factory=GoalParams)
    refusal: VoiceRefusal | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if (self.kind is None) != (self.outcome is not VoiceOutcome.GOAL):
            raise ValueError("a kind belongs to a goal resolution and to nothing else")
        if (self.refusal is None) == (self.outcome is VoiceOutcome.REFUSED):
            raise ValueError("a refusal belongs to a refused resolution and to nothing else")
        if self.kind is None and self.params.present():
            raise ValueError("parameters belong to a kind")

    @property
    def accepted(self) -> bool:
        """True when this utterance became a goal."""
        return self.outcome is VoiceOutcome.GOAL


# --------------------------------------------------------------------------
# vocabularies
# --------------------------------------------------------------------------

#: The complete map from speech to a kind. Every word is stored in the form
#: :func:`~.messages.normalise_transcript` produces — lower case, NFKC, no "ё" —
#: because a table entry that does not survive normalisation is an entry that
#: never matches, and :func:`_check_tables` refuses the module rather than let
#: one sit there looking correct.
KIND_WORDS: Final[Mapping[GoalKind, frozenset[str]]] = MappingProxyType(
    {
        GoalKind.SATISFY_HUNGER: frozenset(
            {
                "поешь",
                "поесть",
                "покушай",
                "покушать",
                "съешь",
                "съесть",
                "перекуси",
                "перекусить",
                "еда",
                "еду",
                "голод",
                "голоден",
                "проголодался",
            }
        ),
        GoalKind.SATISFY_THIRST: frozenset(
            {
                "попей",
                "попить",
                "выпей",
                "выпить",
                "пить",
                "напейся",
                "вода",
                "воду",
                "воды",
                "жажда",
                "жажду",
            }
        ),
        GoalKind.READ_FOR_BOREDOM: frozenset(
            {
                "почитай",
                "почитать",
                "читай",
                "читать",
                "книга",
                "книгу",
                "книги",
                "скука",
                "скучно",
                "развлекись",
            }
        ),
        GoalKind.TRAIN_SKILL: frozenset(
            {
                "прокачай",
                "прокачать",
                "качай",
                "качать",
                "тренируй",
                "тренировать",
                "тренируйся",
                "навык",
                "навыки",
            }
        ),
        GoalKind.LEARN_RECIPE: frozenset(
            {
                "рецепт",
                "рецепту",
                "рецепты",
                "рецептов",
                "выучи",
                "выучить",
                "изучи",
                "изучить",
                "разучи",
            }
        ),
    }
)

#: Which skill a ``train_skill`` utterance named. Single tokens only: a
#: multi-word skill name would need a phrase matcher, and the one skill that
#: wants one — first aid — is reachable through "медицина", which is the word
#: the game's own UI uses in Russian.
SKILL_WORDS: Final[Mapping[TrainableSkill, frozenset[str]]] = MappingProxyType(
    {
        TrainableSkill.CARPENTRY: frozenset(
            {"плотничество", "плотничеству", "плотник", "плотницкое", "столярка"}
        ),
        TrainableSkill.COOKING: frozenset({"готовка", "готовку", "готовке", "кулинария"}),
        TrainableSkill.FARMING: frozenset({"фермерство", "земледелие", "огород", "огородничество"}),
        TrainableSkill.ELECTRICAL: frozenset(
            {"электрика", "электрику", "электричество", "электротехника"}
        ),
        TrainableSkill.METALWORKING: frozenset(
            {"металл", "металлообработка", "металлообработку", "сварка", "сварку"}
        ),
        TrainableSkill.MECHANICS: frozenset({"механика", "механику", "механике"}),
        TrainableSkill.TAILORING: frozenset({"портной", "портняжное", "шитье", "шитью"}),
        TrainableSkill.FORAGING: frozenset({"собирательство", "собирательству"}),
        TrainableSkill.FISHING: frozenset({"рыбалка", "рыбалку", "рыболовство"}),
        TrainableSkill.TRAPPING: frozenset({"ловушки", "ловушкам", "капканы", "траппинг"}),
        TrainableSkill.FIRST_AID: frozenset({"медицина", "медицину", "медицине"}),
    }
)

#: The unit a number was spoken with, and therefore which parameter it is. A
#: number without one of these is not assigned to a parameter by position or by
#: magnitude — guessing that "поешь 5" meant five percent, five pages or level
#: five is exactly the guess this table exists to avoid.
UNIT_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "страница": "pages",
        "страницы": "pages",
        "страниц": "pages",
        "страницу": "pages",
        "стр": "pages",
        "процент": "satisfy_to",
        "процента": "satisfy_to",
        "процентов": "satisfy_to",
        "уровень": "target_level",
        "уровня": "target_level",
        "уровне": "target_level",
        "лвл": "target_level",
    }
)

#: The one kind for which a bare number has an unambiguous meaning: "прокачай
#: механику до 7" is a target level and can be nothing else. Every other kind
#: refuses a unitless number.
BARE_NUMBER_PARAM: Final[Mapping[GoalKind, str]] = MappingProxyType(
    {GoalKind.TRAIN_SKILL: "target_level"}
)

#: Percentages are spoken as whole percent and stored as the fraction the
#: channel declares, so the two ends of the conversion live next to each other.
_PERCENT_DIVISOR: Final = 100.0


def _check_tables() -> None:
    """Refuse to import a module whose tables disagree with each other.

    Every failure below is silent at runtime if it is allowed through: a
    vocabulary word that does not survive normalisation simply never matches, a
    unit naming a parameter the kind does not accept produces a refusal nobody
    can act on, and a stop word that is also a goal word is a safety defect that
    would only show up when somebody shouted it.
    """
    if MAX_UTTERANCE_CHARS > MAX_TRANSCRIPT_CHARS:
        raise RuntimeError("the utterance bound must not exceed the transcript bound")

    missing_kinds = set(GoalKind) - set(KIND_WORDS)
    if missing_kinds:
        raise RuntimeError(f"no vocabulary for {sorted(k.value for k in missing_kinds)}")
    missing_skills = set(TrainableSkill) - set(SKILL_WORDS)
    if missing_skills:
        raise RuntimeError(f"no vocabulary for {sorted(s.value for s in missing_skills)}")

    groups: list[tuple[str, frozenset[str]]] = [
        *((kind.value, words) for kind, words in KIND_WORDS.items()),
        *((skill.value, words) for skill, words in SKILL_WORDS.items()),
        ("units", frozenset(UNIT_WORDS)),
        ("stop", STOP_WORDS),
    ]
    seen: dict[str, str] = {}
    for owner, words in groups:
        for word in words:
            if normalise_transcript(word) != word or _NON_WORD.search(word):
                raise RuntimeError(f"{owner} declares a word that no transcript can match")
            if word in seen:
                raise RuntimeError(f"{owner} and {seen[word]} claim the same word")
            seen[word] = owner

    for parameter in UNIT_WORDS.values():
        if parameter not in NUMERIC_RANGES:
            raise RuntimeError(f"unit maps to {parameter}, which has no declared range")
    for kind, parameter in BARE_NUMBER_PARAM.items():
        spec = GOAL_SPECS[kind]
        if parameter not in spec.required | spec.optional:
            raise RuntimeError(f"{kind.value} does not accept {parameter}")

    for code in VoiceRefusalCode:
        message = REFUSAL_MESSAGES.get(code, "")
        if not message.strip():
            raise RuntimeError(f"{code.value} has no refusal sentence")
        if len(message) > MAX_TEXT_CHARS:
            raise RuntimeError(f"{code.value} is too long to speak")
        if code not in REFUSAL_REASONS:
            raise RuntimeError(f"{code.value} has no reason code")

    longest = mint_idempotency_key(max(GoalKind, key=lambda k: len(k.value)), sequence=0)
    if len(longest) > MAX_IDEMPOTENCY_KEY_LEN:
        raise RuntimeError("a minted idempotency key can exceed the channel's bound")


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------


def bounded_words(utterance: str) -> tuple[tuple[str, ...], bool]:
    """*utterance* as bounded, normalised word tokens, and whether it was cut.

    The bound is applied to the raw string, before normalisation, so every step
    after it is linear in :data:`MAX_UTTERANCE_CHARS` characters. Nothing is
    kept between calls: an utterance too long to match is refused, not buffered
    for the next one.
    """
    truncated = len(utterance) > MAX_UTTERANCE_CHARS
    text = normalise_transcript(utterance[:MAX_UTTERANCE_CHARS])
    return tuple(token for token in _NON_WORD.split(text) if token), truncated


def is_stop_utterance(utterance: str) -> bool:
    """True when the bounded prefix of *utterance* contains a stop word."""
    words, _ = bounded_words(utterance)
    return not STOP_WORDS.isdisjoint(words)


def matched_kinds(words: tuple[str, ...]) -> tuple[GoalKind, ...]:
    """Every kind whose vocabulary *words* touches, in declaration order."""
    spoken = set(words)
    return tuple(kind for kind, words_for in KIND_WORDS.items() if not words_for.isdisjoint(spoken))


def matched_skills(words: tuple[str, ...]) -> tuple[TrainableSkill, ...]:
    """Every skill whose vocabulary *words* touches, in declaration order."""
    spoken = set(words)
    return tuple(
        skill for skill, words_for in SKILL_WORDS.items() if not words_for.isdisjoint(spoken)
    )


def _refused(
    code: VoiceRefusalCode,
    *,
    parameter: str = "",
    allowed: NumericRange | None = None,
    truncated: bool = False,
) -> VoiceResolution:
    return VoiceResolution(
        outcome=VoiceOutcome.REFUSED,
        refusal=VoiceRefusal(code=code, parameter=parameter, allowed=allowed),
        truncated=truncated,
    )


def _spoken_units(words: tuple[str, ...], text: str) -> frozenset[str]:
    """Which parameters the utterance named a unit for."""
    units = {UNIT_WORDS[word] for word in words if word in UNIT_WORDS}
    if _PERCENT_SIGN in text:
        units.add("satisfy_to")
    return frozenset(units)


def _spoken_numbers(words: tuple[str, ...]) -> tuple[str, ...]:
    """Digit tokens, still as text so their length can be judged before ``int``.

    ``str.isdigit`` is true of digits from every script, and ``int`` accepts
    them: the Arabic-Indic digit five is a five to both. ASCII is therefore
    required as well. A non-ASCII digit is left to fall through as
    an unrecognised word rather than silently becoming a parameter.
    """
    return tuple(word for word in words if word.isascii() and word.isdigit())


def _resolve_skill(
    kind: GoalKind, words: tuple[str, ...]
) -> TrainableSkill | VoiceResolution | None:
    """The skill for *kind*, ``None`` when it takes none, or the refusal."""
    spec = GOAL_SPECS[kind]
    accepts_skill = "skill" in spec.required | spec.optional
    skills = matched_skills(words)
    if len(skills) > 1:
        return _refused(VoiceRefusalCode.AMBIGUOUS_SKILL, parameter="skill")
    if skills and not accepts_skill:
        return _refused(VoiceRefusalCode.PARAMETER_NOT_ALLOWED, parameter="skill")
    if not skills and "skill" in spec.required:
        return _refused(VoiceRefusalCode.UNKNOWN_SKILL, parameter="skill")
    return skills[0] if skills else None


def _resolve_number(
    kind: GoalKind, words: tuple[str, ...], text: str
) -> tuple[str, float] | VoiceResolution | None:
    """The named parameter and its value, ``None`` when none was spoken."""
    units = _spoken_units(words, text)
    if len(units) > 1:
        return _refused(VoiceRefusalCode.CONFLICTING_PARAMETERS)
    digits = _spoken_numbers(words)
    if len(digits) > 1:
        return _refused(VoiceRefusalCode.AMBIGUOUS_NUMBER)
    if units and not digits:
        return _refused(VoiceRefusalCode.MISSING_NUMBER, parameter=next(iter(units)))
    if not digits:
        return None

    parameter = next(iter(units)) if units else BARE_NUMBER_PARAM.get(kind)
    if parameter is None:
        return _refused(VoiceRefusalCode.UNITLESS_NUMBER)
    spec = GOAL_SPECS[kind]
    if parameter not in spec.required | spec.optional:
        return _refused(VoiceRefusalCode.PARAMETER_NOT_ALLOWED, parameter=parameter)

    declared = NUMERIC_RANGES[parameter]
    if len(digits[0]) > MAX_NUMBER_DIGITS:
        return _refused(VoiceRefusalCode.OUT_OF_RANGE, parameter=parameter, allowed=declared)
    spoken = int(digits[0])
    value = spoken / _PERCENT_DIVISOR if parameter == "satisfy_to" else float(spoken)
    if not declared.minimum <= value <= declared.maximum:
        return _refused(VoiceRefusalCode.OUT_OF_RANGE, parameter=parameter, allowed=declared)
    return parameter, value


def _build(
    kind: GoalKind, skill: TrainableSkill | None, number: tuple[str, float] | None
) -> VoiceResolution:
    """Assemble the goal, converting any residual range failure into a refusal."""
    parameter, value = ("", 0.0) if number is None else number
    try:
        if parameter == "target_level":
            params = GoalParams(skill=skill, target_level=int(value))
        elif parameter == "pages":
            params = GoalParams(skill=skill, pages=int(value))
        elif parameter == "satisfy_to":
            params = GoalParams(skill=skill, satisfy_to=value)
        else:
            params = GoalParams(skill=skill)
    except ValueError:
        # Discarded deliberately: this branch is only reachable if a range in
        # NUMERIC_RANGES stops agreeing with the check above, and the exception
        # text quotes the value the user spoke. Losing a message that may not be
        # rendered is the smaller loss against rendering a transcript.
        return _refused(VoiceRefusalCode.INTERNAL, parameter=parameter or "")
    return VoiceResolution(outcome=VoiceOutcome.GOAL, kind=kind, params=params)


def resolve(utterance: str) -> VoiceResolution:
    """Resolve *utterance* to a stop, to one goal, or to a named refusal.

    Total: every string returns, none raises, and nothing derived from
    *utterance* appears in what is returned.
    """
    words, truncated = bounded_words(utterance)
    # Before the bound, before the empty check, before any kind: a stop heard as
    # anything else is the one failure in this package that is not recoverable.
    if not STOP_WORDS.isdisjoint(words):
        return VoiceResolution(outcome=VoiceOutcome.STOP, truncated=truncated)
    if truncated:
        return _refused(VoiceRefusalCode.TOO_LONG, truncated=True)
    if not words:
        return _refused(VoiceRefusalCode.EMPTY)

    kinds = matched_kinds(words)
    if len(kinds) > 1:
        return _refused(VoiceRefusalCode.AMBIGUOUS_KIND)
    if not kinds:
        return _refused(VoiceRefusalCode.UNMAPPED)
    kind = kinds[0]

    skill = _resolve_skill(kind, words)
    if isinstance(skill, VoiceResolution):
        return skill
    number = _resolve_number(kind, words, normalise_transcript(utterance[:MAX_UTTERANCE_CHARS]))
    if isinstance(number, VoiceResolution):
        return number
    return _build(kind, skill, number)


# --------------------------------------------------------------------------
# handing a resolution to the goal channel
# --------------------------------------------------------------------------


def mint_idempotency_key(kind: GoalKind, *, sequence: int) -> str:
    """A key for a voice-submitted goal, built from a kind and a counter.

    The goal channel accepts exactly one caller-supplied string, and this is how
    the voice process supplies it without ever having a transcript in hand: the
    key is a constant prefix, an enum value and a zero-padded number.
    """
    if not 0 <= sequence <= MAX_VOICE_SEQUENCE:
        raise ValueError(f"sequence must be within 0..{MAX_VOICE_SEQUENCE}, got {sequence}")
    return f"{_KEY_PREFIX}.{kind.value}.{sequence:06d}"


def to_goal_request(resolution: VoiceResolution, *, sequence: int) -> GoalRequest:
    """*resolution* as a submission to the goal channel.

    Raises:
        ValueError: when *resolution* is not a goal. A stop is not a goal and a
            refusal is not a goal; turning either into one here would be the
            fabricated success AGENTS.md forbids.
    """
    if resolution.outcome is not VoiceOutcome.GOAL or resolution.kind is None:
        raise ValueError("only a goal resolution becomes a goal request")
    return GoalRequest(
        kind=resolution.kind,
        idempotency_key=mint_idempotency_key(resolution.kind, sequence=sequence),
        params=resolution.params,
    )


_check_tables()
