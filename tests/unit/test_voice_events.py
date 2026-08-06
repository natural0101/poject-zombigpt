"""The TTS event stream's bounds and its refusal to be broken by a consumer."""

from __future__ import annotations

import pytest

from pz_agent_voice.events import (
    MAX_FAILURES,
    MAX_SUBSCRIBERS,
    TooManySubscribers,
    TtsEvent,
    TtsEventKind,
    TtsEventStream,
)
from tests.fixtures.voice_doubles import utterance


def an_event(kind: TtsEventKind = TtsEventKind.QUEUED, at_ms: int = 1) -> TtsEvent:
    return TtsEvent(kind=kind, at_ms=at_ms, utterance=utterance())


def test_a_subscriber_receives_every_event_until_it_unsubscribes() -> None:
    stream = TtsEventStream()
    seen: list[TtsEvent] = []
    unsubscribe = stream.subscribe(seen.append)

    stream.publish(an_event())
    unsubscribe()
    stream.publish(an_event(TtsEventKind.FINISHED))

    assert [event.kind for event in seen] == [TtsEventKind.QUEUED]
    assert stream.subscribers == 0


def test_unsubscribing_twice_is_harmless() -> None:
    stream = TtsEventStream()
    unsubscribe = stream.subscribe(lambda _event: None)

    unsubscribe()
    unsubscribe()

    assert stream.subscribers == 0


def test_the_subscriber_count_is_capped() -> None:
    stream = TtsEventStream()
    for _ in range(MAX_SUBSCRIBERS):
        stream.subscribe(lambda _event: None)

    with pytest.raises(TooManySubscribers):
        stream.subscribe(lambda _event: None)

    assert stream.subscribers == MAX_SUBSCRIBERS


def test_a_listener_that_raises_is_dropped_and_the_rest_still_receive() -> None:
    stream = TtsEventStream()
    seen: list[TtsEvent] = []

    def broken(_event: TtsEvent) -> None:
        raise ValueError("consumer bug")

    stream.subscribe(broken)
    stream.subscribe(seen.append)

    stream.publish(an_event())
    stream.publish(an_event(TtsEventKind.STARTED))

    assert len(seen) == 2
    assert stream.subscribers == 1
    assert stream.failures == ("ValueError: consumer bug",)


def test_the_failure_log_is_bounded_and_keeps_the_most_recent() -> None:
    # One broken listener is dropped, so the only way to reach the cap is a
    # succession of them — a HUD reconnecting into a bad state, say. The log is
    # a diagnostic, not an archive, and it must not grow with the session.
    stream = TtsEventStream()

    for index in range(MAX_FAILURES * 3):

        def broken(_event: TtsEvent, index: int = index) -> None:
            raise ValueError(f"consumer bug {index}")

        stream.subscribe(broken)
        stream.publish(an_event())

    assert len(stream.failures) == MAX_FAILURES
    assert stream.failures[-1] == f"ValueError: consumer bug {MAX_FAILURES * 3 - 1}"
    assert stream.subscribers == 0


def test_history_is_a_bounded_ring() -> None:
    stream = TtsEventStream(max_history=4)

    for index in range(20):
        stream.publish(an_event(at_ms=index))

    assert len(stream.history) == 4
    assert [event.at_ms for event in stream.history] == [16, 17, 18, 19]


def test_only_a_cleared_event_may_name_no_utterance() -> None:
    TtsEvent(kind=TtsEventKind.CLEARED, at_ms=1)

    with pytest.raises(ValueError, match="must name the utterance"):
        TtsEvent(kind=TtsEventKind.STARTED, at_ms=1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_subscribers": 0}, "max_subscribers must be positive"),
        ({"max_history": 0}, "max_history must be positive"),
    ],
)
def test_a_stream_with_a_non_positive_bound_is_refused(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TtsEventStream(**kwargs)
