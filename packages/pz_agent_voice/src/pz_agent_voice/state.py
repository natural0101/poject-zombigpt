"""The conversation's states and the record of one turn through them.

Split from :mod:`.session` so that a consumer — a HUD, a test, the diagnostics
bundle — can describe what the companion did without importing the machine that
did it.

``VoiceTurn`` is a report, not a command: it says what happened, including the
cases where nothing did. A turn that produced no utterance is the normal outcome
of most transcripts, and recording it explicitly is what makes "the companion
stayed quiet" an observable behaviour rather than an absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .messages import VoiceGoal, VoiceIntent, VoiceOutput
from .ports import PlanRecord, StopReport

__all__ = ["VoiceState", "VoiceTurn"]


class VoiceState(StrEnum):
    """Where the conversation is.

    ``IDLE`` is not "off": a stop is honoured from it, and always without a wake
    word. What ``IDLE`` withholds is the right to start work.
    """

    IDLE = "idle"
    LISTENING = "listening"
    AWAITING_ANSWER = "awaiting_answer"


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    """What one transcript caused."""

    intent: VoiceIntent
    state: VoiceState
    utterances: tuple[VoiceOutput, ...] = ()
    goal: VoiceGoal | None = None
    plan: PlanRecord | None = None
    stop: StopReport | None = None
    #: True when speech in flight must be cut short, whatever it was saying.
    interrupt_speech: bool = False
    detail: str = ""
