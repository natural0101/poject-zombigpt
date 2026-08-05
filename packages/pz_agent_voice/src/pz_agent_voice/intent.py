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
"""

from __future__ import annotations

# Several of the words below are short enough that every letter in them has a
# Latin lookalike, which reads to the confusable-character rule as mistyped
# ASCII. They are not mistyped: they are the vocabulary this matcher exists to
# recognise, and taking the rule's suggestion would stop each of them matching.
# ruff: noqa: RUF001
from dataclasses import dataclass
from typing import Final

from .messages import VoiceGoal, VoiceInput, VoiceIntent

__all__ = [
    "AFFIRM_WORDS",
    "DEFAULT_WAKE_WORDS",
    "DENY_WORDS",
    "GOAL_WORDS",
    "STATUS_WORDS",
    "STOP_WORDS",
    "IntentMatch",
    "classify",
    "is_stop",
    "matched_goals",
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
