"""The two value types, and the bounds they refuse to be built outside."""

from __future__ import annotations

import pytest

from pz_agent_voice.messages import (
    MAX_TEXT_CHARS,
    MAX_TRANSCRIPT_CHARS,
    OutputKind,
    VoiceInput,
    VoiceOutput,
    normalise_transcript,
)


def test_a_transcript_is_folded_and_bounded_before_it_is_matched() -> None:
    raw = VoiceInput(transcript="  СТОП\t\tи   ВСЁ  ", at_ms=1)

    assert raw.normalised() == "  стоп\t\tи   все  "
    assert raw.words() == ("стоп", "и", "все")


def test_normalisation_truncates_before_it_folds() -> None:
    folded = normalise_transcript("x" * (MAX_TRANSCRIPT_CHARS * 3))

    assert len(folded) == MAX_TRANSCRIPT_CHARS


def test_punctuation_and_hyphens_split_into_separate_words() -> None:
    assert VoiceInput(transcript="стоп-стоп, хватит!", at_ms=1).words() == (
        "стоп",
        "стоп",
        "хватит",
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"at_ms": -1}, "at_ms must be non-negative"),
        ({"at_ms": 1, "confidence": 1.2}, "confidence must be within"),
        ({"at_ms": 1, "confidence": -0.1}, "confidence must be within"),
    ],
)
def test_an_impossible_transcript_is_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        VoiceInput(transcript="стоп", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"topic": ""}, "topic must be a lowercase token"),
        ({"topic": "Plan"}, "topic must be a lowercase token"),
        ({"topic": "plan topic"}, "topic must be a lowercase token"),
        ({"text": "   "}, "must carry something to say"),
        ({"text": "x" * (MAX_TEXT_CHARS + 1)}, "at most"),
        ({"revision": -1}, "revision must be non-negative"),
        ({"at_ms": -1}, "at_ms must be non-negative"),
    ],
)
def test_an_unspeakable_utterance_is_refused(kwargs: dict[str, object], message: str) -> None:
    fields: dict[str, object] = {
        "kind": OutputKind.STATUS,
        "topic": "plan",
        "text": "Готово.",
        "at_ms": 0,
    }
    fields.update(kwargs)
    with pytest.raises(ValueError, match=message):
        VoiceOutput(**fields)  # type: ignore[arg-type]


def test_urgency_ranks_run_from_stop_down_to_status() -> None:
    ranks = [kind.rank for kind in OutputKind]

    assert OutputKind.STOP.rank == 0
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_two_utterances_about_the_same_subject_are_recognised_as_such() -> None:
    first = VoiceOutput(kind=OutputKind.STATUS, topic="plan", text="Готово.", at_ms=0)
    same = VoiceOutput(kind=OutputKind.STATUS, topic="plan", text="Готово.", at_ms=9)
    newer = VoiceOutput(kind=OutputKind.ERROR, topic="plan", text="Не вышло.", at_ms=9)  # noqa: RUF001
    other = VoiceOutput(kind=OutputKind.STATUS, topic="session", text="Готово.", at_ms=9)

    assert same.repeats(first) is True
    assert newer.repeats(first) is False
    assert newer.same_subject(first) is True
    assert other.same_subject(first) is False
