"""The transcript matcher: what it recognises, and what it refuses to guess at."""

from __future__ import annotations

import pytest

from pz_agent_voice.intent import DEFAULT_WAKE_WORDS, classify, is_stop, matched_goals
from pz_agent_voice.messages import VoiceGoal, VoiceInput, VoiceIntent


def said(text: str, *, final: bool = True, confidence: float = 1.0) -> VoiceInput:
    return VoiceInput(transcript=text, at_ms=1_000, final=final, confidence=confidence)


@pytest.mark.parametrize(
    "transcript",
    [
        "стоп",
        "Стоп!",
        "нет, стой",
        "агент, хватит",
        "ну всё, прекрати это",
        "STOP",
        "please stop now",
        "стоп-стоп-стоп",
    ],
)
def test_stop_is_recognised_anywhere_in_a_sentence(transcript: str) -> None:
    assert is_stop(said(transcript)) is True


def test_stop_is_recognised_in_an_interim_transcript() -> None:
    # The whole point of listening to interim hypotheses: waiting for the
    # recogniser to endpoint would add its delay to the stop latency.
    assert is_stop(said("сто", final=False)) is False
    assert is_stop(said("стоп", final=False, confidence=0.1)) is True


@pytest.mark.parametrize("transcript", ["стопка тарелок", "остановка автобуса", "stopwatch"])
def test_a_word_containing_a_stop_word_is_not_a_stop(transcript: str) -> None:
    assert is_stop(said(transcript)) is False


def test_cancel_is_not_a_stop() -> None:
    # "Отмена" declines a clarification question. Reading it as a panic stop
    # would disarm the session every time the user said "no".
    assert is_stop(said("отмена")) is False
    assert classify(said("отмена")).intent is VoiceIntent.DENY


@pytest.mark.parametrize(
    ("transcript", "goal"),
    [
        ("агент, поешь", VoiceGoal.EAT),
        ("попей воды", VoiceGoal.DRINK),
        ("почитай плотницкое", VoiceGoal.READ),
        ("продолжай", VoiceGoal.RESUME),
        ("agent, eat", VoiceGoal.EAT),
    ],
)
def test_one_goal_resolves_unambiguously(transcript: str, goal: VoiceGoal) -> None:
    match = classify(said(transcript))
    assert match.intent is VoiceIntent.GOAL
    assert match.goal is goal


def test_two_goals_are_ambiguous_rather_than_first_wins() -> None:
    match = classify(said("агент, поешь и попей"))
    assert match.intent is VoiceIntent.AMBIGUOUS
    assert match.goals == (VoiceGoal.EAT, VoiceGoal.DRINK)
    assert match.goal is None


def test_a_stop_word_outranks_a_goal_in_the_same_sentence() -> None:
    match = classify(said("поешь... нет, стоп"))
    assert match.intent is VoiceIntent.STOP
    assert match.goals == ()


def test_case_and_the_yo_spelling_do_not_change_the_match() -> None:
    assert matched_goals(said("СЪЕШЬ").words()) == (VoiceGoal.EAT,)
    assert is_stop(said("СТОП")) is True


def test_wake_word_alone_opens_a_session_and_nothing_else() -> None:
    match = classify(said("агент"))
    assert match.intent is VoiceIntent.WAKE
    assert match.woke is True
    assert match.goals == ()


def test_a_goal_without_the_wake_word_still_classifies() -> None:
    # The wake gate lives in the session, not here: classification must report
    # the goal so the session can decide whether it is allowed to act on it.
    match = classify(said("поешь"))
    assert match.intent is VoiceIntent.GOAL
    assert match.woke is False


def test_unknown_speech_matches_nothing() -> None:
    match = classify(said("сегодня довольно пасмурно"))
    assert match.intent is VoiceIntent.UNKNOWN
    assert match.goals == ()


def test_a_transcript_longer_than_the_bound_is_truncated_before_matching() -> None:
    padded = "а" * 5_000 + " поешь"  # noqa: RUF001
    assert matched_goals(said(padded).words()) == ()
    assert len(said(padded).normalised()) <= 400


def test_the_default_wake_words_are_case_folded() -> None:
    assert all(word == word.casefold() for word in DEFAULT_WAKE_WORDS)
