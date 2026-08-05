"""Bounds and thresholds for the voice loop, validated once at construction.

Every number here is a limit rather than a preference, and each is checked when
the config is built so a misconfigured session fails at start-up instead of
behaving oddly an hour in. The blueprint's ``[voice]`` section (§ 11.8) supplies
the two the user actually sets — the adapter and whether the loop runs at all —
and those live in the CLI's config; what remains here is what the loop itself
has to be bounded by.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from .intent import DEFAULT_WAKE_WORDS
from .queue import DEFAULT_MAX_PENDING

__all__ = [
    "DEFAULT_VOICE_CONFIG",
    "MAX_PLAN_REAL_SECONDS",
    "MAX_PLAN_STEPS",
    "VoiceConfig",
]

#: Ceilings on what a config may ask for. A plan the user started by voice is
#: still a plan; these mirror the blueprint's ``pz_plan_execute`` limits (§ 6.15)
#: so the voice path cannot request a longer one than the MCP path can.
MAX_PLAN_STEPS: Final = 8
MAX_PLAN_REAL_SECONDS: Final = 600


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """How the companion listens, and how much it is allowed to say."""

    wake_words: frozenset[str] = field(default_factory=lambda: DEFAULT_WAKE_WORDS)
    #: A wake session that has heard nothing for this long closes, so a stray
    #: word an hour later is not read as a command.
    wake_ttl_ms: int = 30_000
    require_wake_word: bool = True
    #: Below this the transcript is treated as misheard and the user is asked to
    #: repeat. Guessing at a low-confidence command is how an assistant eats the
    #: last tin of beans because it heard "поешь" in "пошли".
    min_confidence: float = 0.6
    max_pending_utterances: int = DEFAULT_MAX_PENDING
    #: How many times one ambiguity may be asked about before the companion
    #: gives up and says nothing further about it. Bounded because a
    #: clarification loop is the most annoying failure this package has.
    max_clarifications: int = 1
    plan_max_steps: int = MAX_PLAN_STEPS
    plan_max_real_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.wake_words and self.require_wake_word:
            raise ValueError("require_wake_word needs at least one wake word")
        if any(word != word.casefold() or not word for word in self.wake_words):
            raise ValueError("wake words must be non-empty and case-folded to match transcripts")
        if self.wake_ttl_ms <= 0:
            raise ValueError(f"wake_ttl_ms must be positive, got {self.wake_ttl_ms}")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(f"min_confidence must be within 0..1, got {self.min_confidence}")
        if self.max_pending_utterances < 1:
            raise ValueError(
                f"max_pending_utterances must be positive, got {self.max_pending_utterances}"
            )
        if self.max_clarifications < 0:
            raise ValueError(
                f"max_clarifications must be non-negative, got {self.max_clarifications}"
            )
        if not 1 <= self.plan_max_steps <= MAX_PLAN_STEPS:
            raise ValueError(
                f"plan_max_steps must be within 1..{MAX_PLAN_STEPS}, got {self.plan_max_steps}"
            )
        if not 1 <= self.plan_max_real_seconds <= MAX_PLAN_REAL_SECONDS:
            raise ValueError(
                f"plan_max_real_seconds must be within 1..{MAX_PLAN_REAL_SECONDS}, "
                f"got {self.plan_max_real_seconds}"
            )


DEFAULT_VOICE_CONFIG: Final = VoiceConfig()
