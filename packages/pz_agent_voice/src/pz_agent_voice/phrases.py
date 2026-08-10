"""Every sentence the companion is allowed to say, as a closed table.

Nothing here is built by interpolating a transcript, an item name or any other
string that came from outside this process. That is the whole reason the table
exists: the microphone is the one input that reaches a loudspeaker directly, and
a template with a free slot in it is the shortest path from "the recogniser
heard something odd" to "the assistant read it out".

The register follows ``examples/blueprint/VOICE_CONVERSATIONS_RU.md``: one
sentence, factual, no filler, no restating what the user just said.
"""

from __future__ import annotations

# Some of the words below are short enough that every letter in them has a Latin
# lookalike, which reads to the confusable-character rule as mistyped ASCII.
# Taking its suggestion would leave a word that is not a word, handed to a
# synthesiser that would then read it out as one.
# ruff: noqa: RUF001
from typing import Final

from pz_agent_core.capabilities import DRINK_CARRIED, EAT_PERCENTAGE, READ_LITERATURE
from pz_agent_core.goals import NUMERIC_RANGES, PARAM_NAMES, GoalKind
from pz_agent_core.protocol import DangerLevel, ReasonCode

# Imported for the totality check at the bottom of this module rather than for
# anything spoken. The direction is the safe one — :mod:`.intent` does not import
# this module — and it buys the guarantee that a capability the matcher can
# refuse on always has a sentence, checked at import instead of at the
# loudspeaker.
from .intent import ALL_VOICE_CAPABILITIES, UNSPEAKABLE_KINDS, UNSPEAKABLE_PARAMS
from .messages import MAX_TEXT_CHARS, IntentRefusal, VoiceGoal

__all__ = [
    "CAPABILITY_NOUNS",
    "CLARIFY_REPEAT",
    "DANGER_PHRASES",
    "GOAL_ACCEPTED",
    "GOAL_NOUNS",
    "KIND_ACCEPTED",
    "NOT_ARMED",
    "NOT_CONNECTED",
    "NOT_UNDERSTOOD",
    "PARAM_NOUNS",
    "PLAN_DONE",
    "PLAN_REFUSED",
    "REFUSAL_SPEECH",
    "STOP_ACK",
    "STOP_FAILED",
    "TAKEOVER",
    "clarify_between",
    "intent_refusal",
    "refusal",
    "status_line",
]

#: Spoken only after the stop has been acknowledged (§ 5.17 step 6).
STOP_ACK: Final = "Остановился."

#: The one failure worth interrupting anything to report: the stop did not take.
STOP_FAILED: Final = "Не смог остановить. Останови вручную."

TAKEOVER: Final = "Управление у тебя. Задачу поставил на паузу."

NOT_UNDERSTOOD: Final = "Не понял."

CLARIFY_REPEAT: Final = "Не расслышал. Повтори."

NOT_CONNECTED: Final = "Нет связи с игрой."

NOT_ARMED: Final = "Автоматизация выключена."

PLAN_DONE: Final = "Готово."

PLAN_REFUSED: Final = "Не получилось."

GOAL_ACCEPTED: Final[dict[VoiceGoal, str]] = {
    VoiceGoal.EAT: "Ищу, что съесть.",
    VoiceGoal.DRINK: "Ищу, что выпить.",
    VoiceGoal.READ: "Ищу, что почитать.",
    VoiceGoal.RESUME: "Продолжаю.",
}

#: Used only to build a clarification between two goals the user may have meant.
GOAL_NOUNS: Final[dict[VoiceGoal, str]] = {
    VoiceGoal.EAT: "поесть",
    VoiceGoal.DRINK: "попить",
    VoiceGoal.READ: "почитать",
    VoiceGoal.RESUME: "продолжить",
}

DANGER_PHRASES: Final[dict[DangerLevel, str]] = {
    DangerLevel.NONE: "спокойно",
    DangerLevel.LOW: "рядом кто-то есть",
    DangerLevel.MEDIUM: "рядом зомби",
    DangerLevel.HIGH: "опасно",
    DangerLevel.CRITICAL: "очень опасно",
}

#: Reasons a user can act on get their own sentence; everything else falls back
#: to :data:`PLAN_REFUSED`. Reading a reason code aloud would be noise — the
#: code belongs in the log, and the log is where a support bundle looks for it.
_REFUSALS: Final[dict[ReasonCode, str]] = {
    ReasonCode.NOT_ARMED: NOT_ARMED,
    ReasonCode.GAME_DISCONNECTED: NOT_CONNECTED,
    ReasonCode.NO_SAFE_FOOD: "Безопасной еды не вижу.",
    ReasonCode.NO_SAFE_DRINK: "Безопасной воды не вижу.",
    ReasonCode.NO_SUITABLE_LITERATURE: "Подходящей книги не вижу.",
    ReasonCode.INVALID_REF: "Предмет пропал, ссылка устарела.",
    ReasonCode.RESOURCE_RESERVED: "Это в резерве.",
    ReasonCode.THREAT_INTERRUPTED: "Прервался, рядом зомби.",
    ReasonCode.USER_TAKEOVER: TAKEOVER,
    ReasonCode.PANIC_STOP: STOP_ACK,
    ReasonCode.POLICY_DENIED: "Не разрешено настройками.",
    ReasonCode.CAPABILITY_UNAVAILABLE: "Эта команда недоступна в этой сборке игры.",
    ReasonCode.SAVE_CHANGED: "Сохранение сменилось, я сбросил ссылки.",
    ReasonCode.SESSION_TERMINATED: "Сессия закрыта.",
    # The three the goal channel reports for a submission that never ran.
    # Each names what the user can do about it: wait, rephrase, or nothing yet.
    ReasonCode.QUEUE_REJECTED: "Я ещё занят прошлой задачей.",
    ReasonCode.INVALID_ARGUMENT: "Не понял значение, скажи иначе.",
    ReasonCode.PRECONDITION_FAILED: "Сейчас это невозможно.",
}


def refusal(reason: ReasonCode | None) -> str:
    """What to say about a plan that ended badly."""
    if reason is None:
        return PLAN_REFUSED
    return _REFUSALS.get(reason, PLAN_REFUSED)


def clarify_between(first: VoiceGoal, second: VoiceGoal) -> str:
    """Ask which of two goals was meant, naming both from the closed table."""
    return f"Уточни: {GOAL_NOUNS[first]} или {GOAL_NOUNS[second]}?"


#: What the companion says when a transcript became a goal on the typed channel.
#: One sentence per kind, so the confirmation is a fact about what was submitted
#: rather than a repetition of what was heard.
KIND_ACCEPTED: Final[dict[GoalKind, str]] = {
    GoalKind.SATISFY_HUNGER: "Ищу, что съесть.",
    GoalKind.SATISFY_THIRST: "Ищу, что выпить.",
    GoalKind.READ_FOR_BOREDOM: "Ищу, что почитать.",
    GoalKind.TRAIN_SKILL: "Ищу книгу по навыку.",
    GoalKind.LEARN_RECIPE: "Ищу книгу с рецептами.",
}

#: How to say each goal parameter, in the nominative singular so one sentence
#: template works for all of them. The names on the left are the core's, so a
#: parameter that is renamed there stops this module importing.
PARAM_NOUNS: Final[dict[str, str]] = {
    "skill": "навык",
    "target_level": "уровень",
    "satisfy_to": "насыщение",
    "pages": "число страниц",
}

#: The unit a range is *spoken* in, and the factor that gets it there. The core
#: holds ``satisfy_to`` as a fraction; nobody says "до нуля целых восьми", so the
#: sentence says eighty per cent and the arithmetic lives here.
_RANGE_SPOKEN_AS: Final[dict[str, tuple[float, str]]] = {
    "target_level": (1.0, ""),
    "satisfy_to": (100.0, " процентов"),
    "pages": (1.0, ""),
}

#: How to say each capability this package can refuse on. The keys are the names
#: :mod:`pz_agent_core.capabilities.probes` publishes and the report on disk
#: carries, which is what makes "названа причина" mean the same thing in the
#: spoken refusal and in the support bundle.
CAPABILITY_NOUNS: Final[dict[str, str]] = {
    EAT_PERCENTAGE: "приём пищи",
    DRINK_CARRIED: "питьё",
    READ_LITERATURE: "чтение",
}

#: The refusals whose whole sentence is a constant. The other three name
#: something — a parameter, a capability — and are built by
#: :func:`intent_refusal` from the closed tables above.
REFUSAL_SPEECH: Final[dict[IntentRefusal, str]] = {
    IntentRefusal.NOT_A_GOAL: (
        "Такой команды не знаю. Скажи: поешь, попей, почитай или прокачай навык."
    ),
    IntentRefusal.AMBIGUOUS_GOAL: "Услышал два задания сразу. Скажи одно.",
    IntentRefusal.SKILL_NOT_NAMED: (
        "Не понял, какой навык. Назови один навык, например плотницкое дело."
    ),
    IntentRefusal.INTERNAL: "Не смог разобрать команду. Повтори её иначе.",
}

#: Refusals that cannot be spoken without being told what to name.
_NAMED_REFUSALS: Final[frozenset[IntentRefusal]] = frozenset(
    {
        IntentRefusal.PARAMETER_OUT_OF_RANGE,
        IntentRefusal.PARAMETER_NOT_ACCEPTED,
        IntentRefusal.CAPABILITY_UNAVAILABLE,
    }
)


def _spoken_number(value: float) -> str:
    """A range bound as a person says it: no trailing zero, no exponent."""
    return f"{value:g}"


def intent_refusal(
    refusal_code: IntentRefusal, *, parameter: str = "", capability: str = ""
) -> str:
    """One sentence naming why a transcript was refused, and what to do instead.

    *parameter* and *capability* are names this process minted — a member of
    :data:`~pz_agent_core.goals.PARAM_NAMES`, a capability from the probe table —
    and they are looked up rather than interpolated. An unknown name raises:
    the only way one can get here is a table in this package having drifted, and
    reading an unrecognised token out of a loudspeaker would be the transcript
    leak the whole module is arranged to prevent.
    """
    if refusal_code is IntentRefusal.PARAMETER_OUT_OF_RANGE:
        bounds = NUMERIC_RANGES[_known_parameter(parameter, ranged=True)]
        factor, unit = _RANGE_SPOKEN_AS[parameter]
        low = _spoken_number(bounds.minimum * factor)
        high = _spoken_number(bounds.maximum * factor)
        noun = PARAM_NOUNS[parameter].capitalize()
        return f"{noun} — от {low} до {high}{unit}. Назови число в этих пределах."
    if refusal_code is IntentRefusal.PARAMETER_NOT_ACCEPTED:
        noun = PARAM_NOUNS[_known_parameter(parameter, ranged=False)]
        return f"Здесь {noun} не задаётся. Повтори без этого."
    if refusal_code is IntentRefusal.CAPABILITY_UNAVAILABLE:
        if capability not in CAPABILITY_NOUNS:
            raise ValueError("a refusal may only name a capability this package knows")
        return f"Не умею в этой сборке игры: {CAPABILITY_NOUNS[capability]}. Сделай это вручную."
    return REFUSAL_SPEECH[refusal_code]


def _known_parameter(name: str, *, ranged: bool) -> str:
    """Return *name* once it is confirmed to be a parameter this package names."""
    table = NUMERIC_RANGES if ranged else PARAM_NOUNS
    if name not in table:
        raise ValueError("a refusal may only name a declared goal parameter")
    return name


def _check_speech_tables() -> None:
    """Refuse to import a table that cannot say something it may be asked to."""
    # The unspeakable kinds and their parameters (see ``intent.UNSPEAKABLE_KINDS``)
    # are excluded on both sides of every comparison: speech can neither start
    # such a goal nor be asked to pronounce its parameters, so a sentence for one
    # would be a row nothing can reach — and a row for one appearing anyway is
    # the two tables disagreeing, which stays an import-time refusal.
    if set(KIND_ACCEPTED) & UNSPEAKABLE_KINDS:
        raise RuntimeError("a kind declared unspeakable has an acceptance sentence anyway")
    missing_kinds = set(GoalKind) - set(KIND_ACCEPTED) - UNSPEAKABLE_KINDS
    if missing_kinds:
        raise RuntimeError(f"nothing to say for goal kind(s) {sorted(missing_kinds)}")
    if set(PARAM_NOUNS) & UNSPEAKABLE_PARAMS:
        raise RuntimeError("a parameter declared unspeakable has a spoken noun anyway")
    if set(PARAM_NOUNS) | UNSPEAKABLE_PARAMS != set(PARAM_NAMES):
        raise RuntimeError("PARAM_NOUNS has drifted from the core's parameter names")
    if set(_RANGE_SPOKEN_AS) & UNSPEAKABLE_PARAMS:
        raise RuntimeError("a parameter declared unspeakable has a spoken range anyway")
    if set(_RANGE_SPOKEN_AS) | UNSPEAKABLE_PARAMS != set(NUMERIC_RANGES):
        raise RuntimeError("_RANGE_SPOKEN_AS has drifted from NUMERIC_RANGES")
    if set(CAPABILITY_NOUNS) != ALL_VOICE_CAPABILITIES:
        raise RuntimeError("a capability the matcher can refuse on has no spoken form")
    unspeakable = set(IntentRefusal) - set(REFUSAL_SPEECH) - _NAMED_REFUSALS
    if unspeakable:
        raise RuntimeError(f"no sentence for refusal(s) {sorted(unspeakable)}")
    overlap = set(REFUSAL_SPEECH) & _NAMED_REFUSALS
    if overlap:
        raise RuntimeError(f"refusal(s) {sorted(overlap)} have two spoken forms")
    # Every sentence a refusal can produce, enumerated — the named ones over
    # every name they can be given — and held against the queue's own bound. A
    # sentence past MAX_TEXT_CHARS is truncated on the way to the synthesiser,
    # and the user then hears half of what went wrong and none of what to say
    # instead, which is worse than the refusal not existing.
    spoken_forms: list[tuple[str, str]] = [
        (code.value, sentence) for code, sentence in REFUSAL_SPEECH.items()
    ]
    spoken_forms += [
        (
            f"parameter_out_of_range:{name}",
            intent_refusal(IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter=name),
        )
        # The unspeakable parameters have no spoken range: the matcher can
        # never bind a number to one, so no refusal will ever be asked to say
        # its bounds — and intent_refusal refuses the name outright.
        for name in NUMERIC_RANGES
        if name not in UNSPEAKABLE_PARAMS
    ]
    spoken_forms += [
        (
            f"parameter_not_accepted:{name}",
            intent_refusal(IntentRefusal.PARAMETER_NOT_ACCEPTED, parameter=name),
        )
        for name in PARAM_NOUNS
    ]
    spoken_forms += [
        (
            f"capability_unavailable:{name}",
            intent_refusal(IntentRefusal.CAPABILITY_UNAVAILABLE, capability=name),
        )
        for name in CAPABILITY_NOUNS
    ]
    for owner, sentence in spoken_forms:
        if not sentence.strip():
            raise RuntimeError(f"{owner} has no refusal sentence")
        if len(sentence) > MAX_TEXT_CHARS:
            raise RuntimeError(f"the sentence for {owner} is too long to speak")


_check_speech_tables()


def status_line(*, armed: bool, connected: bool, danger: DangerLevel) -> str:
    """One sentence of session status.

    Assembled from three closed vocabularies, so it says what the session is
    without quoting anything the game or the recogniser produced.
    """
    if not connected:
        return NOT_CONNECTED
    state = "работаю" if armed else "жду команды"
    return f"{state.capitalize()}, {DANGER_PHRASES[danger]}."
