"""The two values the voice loop exchanges, and the closed vocabularies around them.

The invariant this module holds is that *nothing a microphone produced ever
becomes a field the rest of the system acts on*. A :class:`VoiceInput` carries
the user's words, but every consumer reads them through
:meth:`VoiceInput.normalised`, which bounds and flattens the string before a
matcher touches it; the only things that leave the voice layer are members of
:class:`VoiceGoal` and the rest of the enums here.

:class:`VoiceOutput` is deliberately not "a string to say". It carries a
``topic``, which is the dedup key the utterance queue collapses on, and a
``revision``, which is how a later message about the same subject supersedes an
earlier one instead of queueing behind it. Speech that cannot be superseded is
speech that arrives late and wrong.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_TEXT_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "IntentRefusal",
    "OutputKind",
    "VoiceGoal",
    "VoiceInput",
    "VoiceIntent",
    "VoiceOutput",
    "normalise_transcript",
]

#: Longest transcript any matcher will look at. Speech recognisers occasionally
#: emit a whole paragraph of accumulated context; matching is linear in this
#: length, so it is capped before the scan rather than trusted.
MAX_TRANSCRIPT_CHARS: Final = 400

#: Longest utterance the queue will carry. The register is "short and factual",
#: and an assistant that reads out a paragraph is the thing this package exists
#: to avoid.
MAX_TEXT_CHARS: Final = 240

#: Topics are minted by this package, never by a transcript, so they are checked
#: for shape and refused when they fail rather than sanitised.
_TOPIC: Final = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")

_NON_WORD: Final = re.compile(r"[^\w]+", re.UNICODE)


class VoiceIntent(StrEnum):
    """What a transcript turned out to be, once classified.

    ``PARTIAL`` is not an intent the user expressed; it records that the
    transcript was not final and therefore only the stop path was allowed to
    act on it.
    """

    STOP = "stop"
    GOAL = "goal"
    STATUS = "status"
    AFFIRM = "affirm"
    DENY = "deny"
    WAKE = "wake"
    AMBIGUOUS = "ambiguous"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class VoiceGoal(StrEnum):
    """The complete set of goals a transcript may become.

    This is the whole width of the channel from a microphone to the planner: a
    transcript selects one of these tokens or it selects nothing. There is no
    branch anywhere in this package that forwards transcript text as a goal.
    """

    EAT = "eat"
    DRINK = "drink"
    READ = "read"
    RESUME = "resume"


class IntentRefusal(StrEnum):
    """Why a transcript did not become a goal kind.

    A member of this enum is the *only* thing
    :func:`~.intent.resolve_goal` may report when it declines, and that is the
    whole point: the alternative to a named refusal is either silence or a kind
    the matcher invented to have something to return. Both are worse than
    telling the user which part of what they said could not be used.

    Every member has a sentence in :data:`~.phrases.REFUSAL_SPEECH` or a builder
    that composes one from a name this process minted; :func:`~.phrases.
    intent_refusal` refuses to exist without one, checked at import.
    """

    NOT_A_GOAL = "not_a_goal"
    AMBIGUOUS_GOAL = "ambiguous_goal"
    SKILL_NOT_NAMED = "skill_not_named"
    PARAMETER_OUT_OF_RANGE = "parameter_out_of_range"
    PARAMETER_NOT_ACCEPTED = "parameter_not_accepted"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    #: The defensive member: the resolver's own range check accepted a number
    #: the core's constructor then refused, which can only mean the two tables
    #: stopped agreeing. The refusal is a constant so that the constructor's
    #: message — which quotes the number the user spoke — never travels.
    INTERNAL = "internal"


class OutputKind(StrEnum):
    """How urgent an utterance is, which is also its queue ordering."""

    STOP = "stop"
    ERROR = "error"
    QUESTION = "question"
    CONFIRMATION = "confirmation"
    STATUS = "status"

    @property
    def rank(self) -> int:
        """Ordering key; lower is spoken first and evicted last."""
        return _KIND_RANK[self]


_KIND_RANK: Final[dict[OutputKind, int]] = {
    OutputKind.STOP: 0,
    OutputKind.ERROR: 1,
    OutputKind.QUESTION: 2,
    OutputKind.CONFIRMATION: 3,
    OutputKind.STATUS: 4,
}


def normalise_transcript(raw: str) -> str:
    """Bound, fold and flatten *raw* into something a word matcher can scan.

    Case folding, NFKC and the yo-to-ye fold all exist so that one spelling of a
    stop word cannot slip past the stop matcher: recognisers differ on whether
    they emit the diaeresis, and a matcher that only knows one spelling silently
    stops working when the recogniser is swapped. The truncation happens first,
    so everything after it is linear in a bounded string.
    """
    text = unicodedata.normalize("NFKC", raw[:MAX_TRANSCRIPT_CHARS]).casefold()
    # Both letters of the pair are Cyrillic and deliberately so; the
    # confusable-character rule would rewrite one to Latin and break the fold.
    return text.replace("ё", "е")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class VoiceInput:
    """One transcript from the recogniser. Untrusted text, never an instruction.

    ``final`` distinguishes a settled transcript from an interim hypothesis.
    Interim ones still matter — they are what makes stop fast and barge-in
    possible — but nothing except stop is allowed to act on a guess.
    """

    transcript: str
    at_ms: int
    final: bool = True
    confidence: float = 1.0
    source: str = "stt"

    def __post_init__(self) -> None:
        if self.at_ms < 0:
            raise ValueError(f"at_ms must be non-negative, got {self.at_ms}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within 0..1, got {self.confidence}")

    def normalised(self) -> str:
        """The bounded, folded form every matcher in this package reads."""
        return normalise_transcript(self.transcript)

    def words(self) -> tuple[str, ...]:
        """Word tokens of the normalised transcript.

        Token equality rather than substring search is what keeps "стопка" from
        reading as "стоп" — a false stop is harmless, but a matcher that fires
        on substrings also fires on the wrong half of a compound word, and the
        same rule then admits false *goals*, which are not harmless.
        """
        return tuple(token for token in _NON_WORD.split(self.normalised()) if token)


@dataclass(frozen=True, slots=True)
class VoiceOutput:
    """One thing to say.

    ``topic`` is the identity of the *subject*, not of the message: two
    utterances sharing a topic are two statements about the same thing, and the
    queue keeps only the later one. ``revision`` orders them, so a stale message
    produced by a slower path cannot overwrite a fresher one.
    """

    kind: OutputKind
    topic: str
    text: str
    at_ms: int
    revision: int = 0

    def __post_init__(self) -> None:
        if not _TOPIC.match(self.topic):
            raise ValueError(f"topic must be a lowercase token, got {self.topic!r}")
        if not self.text.strip():
            raise ValueError("an utterance must carry something to say")
        if len(self.text) > MAX_TEXT_CHARS:
            raise ValueError(
                f"an utterance may be at most {MAX_TEXT_CHARS} characters, got {len(self.text)}"
            )
        if self.revision < 0:
            raise ValueError(f"revision must be non-negative, got {self.revision}")
        if self.at_ms < 0:
            raise ValueError(f"at_ms must be non-negative, got {self.at_ms}")

    def same_subject(self, other: VoiceOutput) -> bool:
        """True when *other* speaks about the same thing this one does."""
        return self.topic == other.topic

    def repeats(self, other: VoiceOutput) -> bool:
        """True when saying this after *other* would say nothing new."""
        return self.kind is other.kind and self.topic == other.topic and self.text == other.text
