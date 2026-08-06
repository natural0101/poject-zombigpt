"""The async driver: barge-in, stop mid-sentence, and the TTS event stream.

Nothing here sleeps. The companion is synchronised on either an event the fake
adapter sets when it actually begins speaking, or a bounded number of scheduler
turns; the injected clock never moves unless a test moves it.
"""

from __future__ import annotations

import asyncio

import pytest

from pz_agent_core.protocol import ActionStatus
from pz_agent_mcp.ports import PlanRecord
from pz_agent_voice import phrases
from pz_agent_voice.driver import MAX_SPEECH_FAILURES, MAX_TURN_HISTORY, VoiceCompanion
from pz_agent_voice.events import TtsEventKind
from pz_agent_voice.messages import OutputKind, VoiceGoal, VoiceIntent
from tests.fixtures.voice_doubles import (
    NeverOpeningAdapter,
    make_harness,
    settle,
    utterance,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# stop, from every state the driver can be in
# --------------------------------------------------------------------------


async def test_stop_mid_sentence_cancels_the_utterance_and_reaches_the_port() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    speaking = await harness.adapter.wait_until_speaking()
    assert speaking.text == phrases.GOAL_ACCEPTED[VoiceGoal.EAT]

    harness.adapter.push("стоп")
    await settle()

    assert harness.doubles.session.stops == 1
    assert harness.adapter.cancelled == [speaking]
    assert harness.texts()[-1] == phrases.STOP_ACK
    await harness.finish()


async def test_stop_mid_sentence_is_acknowledged_only_after_the_cancellation() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, почитай")
    await harness.start()
    await harness.adapter.wait_until_speaking()

    harness.adapter.push("стоп")
    await settle()

    # The interrupted confirmation ends cancelled; the acknowledgement is a
    # separate, later utterance rather than the same one carrying on.
    assert harness.kinds(topic="plan") == [
        TtsEventKind.QUEUED,
        TtsEventKind.STARTED,
        TtsEventKind.CANCELLED,
    ]
    assert harness.kinds(topic="stop") == [TtsEventKind.QUEUED, TtsEventKind.STARTED]
    assert harness.adapter.started[-1].kind is OutputKind.STOP
    await harness.finish()


async def test_stop_from_idle_needs_no_wake_word_and_nothing_in_flight() -> None:
    harness = make_harness()
    harness.adapter.push("стоп")
    await harness.start()
    await settle()

    assert harness.doubles.session.stops == 1
    assert harness.texts() == [phrases.STOP_ACK]
    await harness.finish()


async def test_stop_while_a_plan_is_running_clears_the_plan_state() -> None:
    harness = make_harness()
    harness.adapter.push("агент, поешь")
    await harness.start()
    await settle()
    running = harness.session.plan_active
    assert running is True

    harness.adapter.push("стоп")
    await settle()

    assert harness.session.plan_active is False
    assert harness.doubles.session.stops == 1
    await harness.finish()


async def test_an_interim_stop_transcript_still_stops() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    await harness.adapter.wait_until_speaking()

    harness.adapter.push("сто", final=False, confidence=0.2)
    harness.adapter.push("стоп", final=False, confidence=0.2)
    await settle()

    assert harness.doubles.session.stops == 1
    await harness.finish()


# --------------------------------------------------------------------------
# barge-in
# --------------------------------------------------------------------------


async def test_any_speech_interrupts_the_current_utterance() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    speaking = await harness.adapter.wait_until_speaking()

    harness.adapter.push("подожди секунду")
    await settle()

    assert harness.adapter.cancel_calls == 1
    assert harness.adapter.cancelled == [speaking]
    assert harness.adapter.spoken == []
    await harness.finish()


async def test_an_interim_transcript_barges_in_without_starting_work() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    await harness.adapter.wait_until_speaking()
    submitted = len(harness.doubles.plans.requests)

    harness.adapter.push("э-э", final=False)
    await settle()

    assert harness.adapter.cancel_calls == 1
    assert len(harness.doubles.plans.requests) == submitted
    assert harness.companion.last_turn is not None
    assert harness.companion.last_turn.intent is VoiceIntent.PARTIAL
    await harness.finish()


async def test_barge_in_with_nothing_being_spoken_costs_nothing() -> None:
    harness = make_harness()
    harness.adapter.push("агент")
    await harness.start()
    await settle()

    assert harness.adapter.cancel_calls == 0
    await harness.finish()


async def test_an_utterance_that_finishes_naturally_is_not_reported_cancelled() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    spoken = await harness.adapter.wait_until_speaking()
    harness.adapter.finish_speech()
    await settle()

    assert harness.adapter.spoken == [spoken]
    assert harness.adapter.cancelled == []
    assert TtsEventKind.FINISHED in harness.kinds()
    await harness.finish()


# --------------------------------------------------------------------------
# the event stream
# --------------------------------------------------------------------------


async def test_the_event_stream_reports_the_whole_life_of_an_utterance() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    await harness.adapter.wait_until_speaking()
    harness.adapter.finish_speech()
    await settle()

    assert harness.kinds(topic="plan") == [
        TtsEventKind.QUEUED,
        TtsEventKind.STARTED,
        TtsEventKind.FINISHED,
    ]
    await harness.finish()


async def test_a_message_superseded_while_the_mouth_is_busy_is_never_heard() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    first = await harness.adapter.wait_until_speaking()

    harness.session.queue.push(
        utterance(OutputKind.STATUS, topic="session", text="Первое", revision=1)
    )
    harness.session.queue.push(
        utterance(OutputKind.STATUS, topic="session", text="Второе", revision=2)
    )
    harness.adapter.finish_speech()
    await settle()

    assert harness.texts() == [first.text, "Второе"]
    assert TtsEventKind.SUPERSEDED in harness.kinds(topic="session")
    await harness.finish()


async def test_speech_the_sidecar_enqueued_is_spoken_without_a_transcript() -> None:
    # Nothing is said into the microphone here at all: the plan finishing is the
    # only event, and it has to reach the loudspeaker on its own.
    harness = make_harness()
    await harness.start()

    harness.session.report_plan(
        PlanRecord(plan_id="plan-1", status=ActionStatus.SUCCEEDED, step_index=1)
    )
    await settle()

    assert harness.texts() == [phrases.PLAN_DONE]
    await harness.finish()


async def test_the_turn_history_is_bounded() -> None:
    harness = make_harness()
    await harness.start()
    for index in range(200):
        harness.adapter.push(f"ничего особенного номер {index}")
    await settle()

    assert len(harness.companion.turns) == MAX_TURN_HISTORY
    await harness.finish()


async def test_the_companion_shuts_down_when_the_stream_ends() -> None:
    harness = make_harness()
    harness.adapter.push("агент, поешь")
    await harness.start()
    await settle()

    await harness.finish()

    assert harness.task is not None
    assert harness.task.done()
    assert harness.companion.speaking is None


# --------------------------------------------------------------------------
# a synthesiser that breaks
# --------------------------------------------------------------------------


async def test_a_synthesiser_that_raises_does_not_silence_the_companion() -> None:
    harness = make_harness()
    harness.adapter.speech_error = RuntimeError("the TTS backend died")
    harness.adapter.push("агент, поешь")
    await harness.start()
    await settle()

    # The failed sentence is reported cancelled — it was not heard — and the
    # pump is still running, so the stop that comes next is still spoken.
    assert harness.kinds(topic="plan") == [
        TtsEventKind.QUEUED,
        TtsEventKind.STARTED,
        TtsEventKind.CANCELLED,
    ]
    assert harness.companion.speech_failures == ("RuntimeError: the TTS backend died",)

    harness.adapter.speech_error = None
    harness.adapter.push("стоп")
    await settle()

    assert harness.doubles.session.stops == 1
    assert harness.adapter.spoken[-1].text == phrases.STOP_ACK
    await harness.finish()


async def test_the_speech_failure_log_is_bounded() -> None:
    harness = make_harness()
    harness.adapter.speech_error = RuntimeError("the TTS backend died")
    await harness.start()
    for index in range(MAX_SPEECH_FAILURES * 3):
        harness.session.queue.push(
            utterance(OutputKind.STATUS, topic=f"t{index}", text=f"line {index}")
        )
        await settle(2)

    assert len(harness.companion.speech_failures) == MAX_SPEECH_FAILURES
    await harness.finish()


async def test_a_cancelled_pump_reports_the_sentence_it_was_saying_as_cancelled() -> None:
    harness = make_harness(hold_speech=True)
    harness.adapter.push("агент, поешь")
    await harness.start()
    await harness.adapter.wait_until_speaking()
    assert harness.task is not None

    harness.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await harness.task

    # STARTED with no terminal event would leave the history claiming the
    # companion is still talking.
    assert harness.kinds(topic="plan")[-1] is TtsEventKind.CANCELLED


async def test_the_companion_gives_its_subscription_back_even_when_the_stream_fails() -> None:
    harness = make_harness()
    companion = VoiceCompanion(
        NeverOpeningAdapter(clock=harness.clock), harness.session, clock=harness.clock
    )
    before = harness.session.events.subscribers

    with pytest.raises(RuntimeError, match="never opened"):
        await companion.run()

    # A leaked subscription is a slot the next companion cannot have, and the
    # stream's cap is small enough for that to be a real limit.
    assert harness.session.events.subscribers == before - 1


async def test_deliver_can_be_driven_without_the_stream() -> None:
    # The panic hotkey path: one input, no adapter stream involved.
    harness = make_harness()
    raw = harness.adapter.push("стоп")

    turn = await harness.companion.deliver(raw)

    assert turn.intent is VoiceIntent.STOP
    assert harness.doubles.session.stops == 1
