"""The utterance queue: what it collapses, what it drops, and in what order.

Every test here is about something the user does *not* hear. That is the point
of the module: an assistant that queues everything it was ever told to say
becomes unusable within a minute, and the collapse rules are what stop that
happening.
"""

from __future__ import annotations

import pytest

from pz_agent_voice.events import TtsEventKind, TtsEventStream
from pz_agent_voice.messages import OutputKind
from pz_agent_voice.queue import DEFAULT_MAX_PENDING, UtteranceQueue
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.voice_doubles import utterance


def make_queue(max_pending: int = DEFAULT_MAX_PENDING) -> tuple[UtteranceQueue, TtsEventStream]:
    events = TtsEventStream()
    return UtteranceQueue(clock=FakeClock(), events=events, max_pending=max_pending), events


def test_an_identical_message_collapses_instead_of_queueing() -> None:
    queue, events = make_queue()
    first = utterance(topic="plan", text="Готово.")
    queue.push(first)

    outcome = queue.push(utterance(topic="plan", text="Готово."))

    assert outcome is TtsEventKind.COLLAPSED
    assert len(queue) == 1
    assert queue.pending == (first,)
    assert events.history[-1].kind is TtsEventKind.COLLAPSED


def test_fifty_identical_reports_produce_one_pending_utterance() -> None:
    # A caller polling an unchanged status is the normal case, not a bug; the
    # queue is what makes it silent rather than a stream of repetitions.
    queue, _ = make_queue()
    for _ in range(50):
        queue.push(utterance(topic="plan", text="Читаю."))

    assert len(queue) == 1


def test_a_newer_message_about_the_same_subject_replaces_the_pending_one() -> None:
    queue, events = make_queue()
    queue.push(utterance(topic="plan", text="Ищу еду.", revision=1))

    outcome = queue.push(utterance(topic="plan", text="Готово.", revision=2))

    assert outcome is TtsEventKind.SUPERSEDED
    assert [message.text for message in queue.pending] == ["Готово."]
    assert events.history[-1].utterance is not None
    assert events.history[-1].utterance.text == "Готово."


def test_superseding_keeps_the_original_position_in_the_queue() -> None:
    queue, _ = make_queue()
    queue.push(utterance(OutputKind.STATUS, topic="a", text="first", revision=1))
    queue.push(utterance(OutputKind.STATUS, topic="b", text="second", revision=1))

    queue.push(utterance(OutputKind.STATUS, topic="a", text="first-updated", revision=2))

    # Replacing in place, not re-queueing: an update about "a" must not lose its
    # turn to "b", which arrived later and has been waiting less.
    assert [message.text for message in queue.pending] == ["first-updated", "second"]


def test_a_stale_revision_is_dropped_rather_than_overwriting_fresher_news() -> None:
    queue, events = make_queue()
    queue.push(utterance(topic="plan", text="Готово.", revision=7))

    outcome = queue.push(utterance(topic="plan", text="Ищу еду.", revision=3))

    # DROPPED, not COLLAPSED: nothing else is going to say what it said, which
    # is exactly what a consumer distinguishing the two needs to know.
    assert outcome is TtsEventKind.DROPPED
    assert events.history[-1].kind is TtsEventKind.DROPPED
    assert [message.text for message in queue.pending] == ["Готово."]


def test_an_equal_revision_about_the_same_subject_still_supersedes() -> None:
    # Two messages minted in the same revision are both current; the later one
    # is the one to say, and treating it as stale would silence fresh news.
    queue, _ = make_queue()
    queue.push(utterance(topic="plan", text="Ищу еду.", revision=4))

    outcome = queue.push(utterance(topic="plan", text="Готово.", revision=4))

    assert outcome is TtsEventKind.SUPERSEDED
    assert [message.text for message in queue.pending] == ["Готово."]


def test_urgency_decides_the_speaking_order_not_arrival() -> None:
    queue, _ = make_queue()
    queue.push(utterance(OutputKind.STATUS, topic="a", text="status"))
    queue.push(utterance(OutputKind.CONFIRMATION, topic="b", text="ok"))
    queue.push(utterance(OutputKind.STOP, topic="stop", text="Остановился."))

    assert queue.pop() is not None
    assert [message.kind for message in queue.pending] == [
        OutputKind.CONFIRMATION,
        OutputKind.STATUS,
    ]


def test_the_queue_never_holds_more_than_its_bound() -> None:
    queue, _ = make_queue(max_pending=3)
    for index in range(20):
        queue.push(utterance(OutputKind.STATUS, topic=f"t{index}", text=f"line {index}"))

    assert len(queue) == 3
    # Equal urgency all the way down, so the newcomer never displaces anything:
    # the three that got in first are the three that are still there, and the
    # seventeen that arrived at a full queue were refused, not silently kept.
    assert [message.topic for message in queue.pending] == ["t0", "t1", "t2"]


def test_at_the_bound_the_least_urgent_oldest_message_is_evicted() -> None:
    queue, events = make_queue(max_pending=2)
    queue.push(utterance(OutputKind.STATUS, topic="old", text="old status"))
    queue.push(utterance(OutputKind.STATUS, topic="new", text="new status"))

    outcome = queue.push(utterance(OutputKind.ERROR, topic="err", text="Не получилось."))  # noqa: RUF001

    assert outcome is TtsEventKind.QUEUED
    assert [message.topic for message in queue.pending] == ["err", "new"]
    dropped = [event for event in events.history if event.kind is TtsEventKind.DROPPED]
    assert len(dropped) == 1
    assert dropped[0].utterance is not None
    assert dropped[0].utterance.topic == "old"


def test_a_less_urgent_newcomer_is_refused_rather_than_evicting_an_error() -> None:
    queue, _ = make_queue(max_pending=2)
    queue.push(utterance(OutputKind.ERROR, topic="e1", text="first error"))
    queue.push(utterance(OutputKind.ERROR, topic="e2", text="second error"))

    outcome = queue.push(utterance(OutputKind.STATUS, topic="chatter", text="Спокойно."))

    assert outcome is TtsEventKind.DROPPED
    assert [message.topic for message in queue.pending] == ["e1", "e2"]


def test_a_stop_acknowledgement_cannot_be_evicted() -> None:
    queue, _ = make_queue(max_pending=1)
    queue.push(utterance(OutputKind.STOP, topic="stop", text="Остановился."))

    for index in range(5):
        queue.push(utterance(OutputKind.ERROR, topic=f"e{index}", text=f"error {index}"))

    assert [message.kind for message in queue.pending] == [OutputKind.STOP]


def test_clear_empties_the_queue_and_says_how_much_it_discarded() -> None:
    queue, events = make_queue()
    queue.push(utterance(topic="a", text="one"))
    queue.push(utterance(topic="b", text="two"))

    assert queue.clear() == 2
    assert queue.pop() is None
    assert events.history[-1].kind is TtsEventKind.CLEARED
    assert events.history[-1].utterance is None


def test_clearing_an_empty_queue_publishes_nothing() -> None:
    queue, events = make_queue()

    assert queue.clear() == 0
    assert events.history == ()


def test_a_non_positive_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="max_pending must be positive"):
        UtteranceQueue(clock=FakeClock(), max_pending=0)
