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

from pz_agent_core.protocol import DangerLevel, ReasonCode

from .messages import VoiceGoal

__all__ = [
    "CLARIFY_REPEAT",
    "DANGER_PHRASES",
    "GOAL_ACCEPTED",
    "GOAL_NOUNS",
    "NOT_ARMED",
    "NOT_CONNECTED",
    "NOT_UNDERSTOOD",
    "PLAN_DONE",
    "PLAN_REFUSED",
    "STOP_ACK",
    "STOP_FAILED",
    "TAKEOVER",
    "clarify_between",
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
}


def refusal(reason: ReasonCode | None) -> str:
    """What to say about a plan that ended badly."""
    if reason is None:
        return PLAN_REFUSED
    return _REFUSALS.get(reason, PLAN_REFUSED)


def clarify_between(first: VoiceGoal, second: VoiceGoal) -> str:
    """Ask which of two goals was meant, naming both from the closed table."""
    return f"Уточни: {GOAL_NOUNS[first]} или {GOAL_NOUNS[second]}?"


def status_line(*, armed: bool, connected: bool, danger: DangerLevel) -> str:
    """One sentence of session status.

    Assembled from three closed vocabularies, so it says what the session is
    without quoting anything the game or the recogniser produced.
    """
    if not connected:
        return NOT_CONNECTED
    state = "работаю" if armed else "жду команды"
    return f"{state.capitalize()}, {DANGER_PHRASES[danger]}."
