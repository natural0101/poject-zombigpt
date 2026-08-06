"""The two adapters: the fake the suite runs on, and the TeamON plugin.

The TeamON tests never touch the vendor SDK — it is not installed and its
surface is unverified — so they exercise the adapter against the client
interface the adapter itself defines. What is actually being checked is the
adapter's own share of the work: clamping a value the client reported out of
range, bounding a transcript, and deciding that a synthesis call which returned
after an interrupt did not deliver its sentence.
"""

from __future__ import annotations

import asyncio

import pytest

from pz_agent_voice.adapter import SpeechCancelled
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.adapters.teamon import (
    TEAMON_SDK_MODULE,
    TeamONSdkUnavailable,
    TeamONTranscript,
    TeamONVoiceAdapter,
    require_teamon_sdk,
    teamon_sdk_available,
)
from pz_agent_voice.messages import MAX_TRANSCRIPT_CHARS, OutputKind
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.voice_doubles import FakeTeamONClient, settle, utterance

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# the fake
# --------------------------------------------------------------------------


async def test_the_fake_stream_delivers_what_was_scripted_and_then_ends() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock())
    adapter.script("агент", "поешь")
    adapter.close()

    stream = await adapter.events()
    delivered = [raw.transcript async for raw in stream]

    assert delivered == ["агент", "поешь"]


async def test_the_fake_records_what_it_was_asked_to_say() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock())
    message = utterance(text="Готово.")

    await adapter.speak(message)

    assert adapter.started == [message]
    assert adapter.spoken == [message]
    assert adapter.cancelled == []


async def test_cancelling_a_held_utterance_reports_it_cancelled() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock(), hold_speech=True)
    message = utterance(text="Читаю.")

    speaking = asyncio.create_task(adapter.speak(message))
    await adapter.wait_until_speaking()

    await adapter.cancel_speech()

    with pytest.raises(SpeechCancelled):
        await speaking
    assert adapter.cancelled == [message]
    assert adapter.spoken == []


async def test_waiting_to_speak_without_holding_is_a_programming_error() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock())

    with pytest.raises(RuntimeError, match="hold_speech"):
        await adapter.wait_until_speaking()


async def test_cancelling_when_nothing_is_in_flight_is_counted_but_harmless() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock(), hold_speech=True)

    await adapter.cancel_speech()

    assert adapter.cancel_calls == 1
    assert adapter.cancelled == []


async def test_the_fake_truncates_an_over_long_transcript_at_the_boundary() -> None:
    adapter = FakeVoiceAdapter(clock=FakeClock())

    raw = adapter.push("а" * 10_000)  # noqa: RUF001

    assert len(raw.transcript) == MAX_TRANSCRIPT_CHARS


# --------------------------------------------------------------------------
# the TeamON plugin
# --------------------------------------------------------------------------


async def test_transcripts_become_voice_inputs() -> None:
    client = FakeTeamONClient(
        [
            TeamONTranscript(text="агент", at_ms=10, final=False, confidence=0.4),
            TeamONTranscript(text="поешь", at_ms=20),
        ]
    )
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    stream = await adapter.events()
    delivered = [raw async for raw in stream]

    assert [raw.transcript for raw in delivered] == ["агент", "поешь"]
    assert [raw.final for raw in delivered] == [False, True]
    assert delivered[0].source == "teamon"


async def test_a_confidence_outside_the_range_is_clamped_not_trusted() -> None:
    client = FakeTeamONClient(
        [
            TeamONTranscript(text="поешь", at_ms=1, confidence=1.7),
            TeamONTranscript(text="попей", at_ms=2, confidence=-0.5),
        ]
    )
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    stream = await adapter.events()
    delivered = [raw.confidence async for raw in stream]

    # An over-range confidence would otherwise sail past the gate that stops the
    # companion acting on a guess; a negative one would be refused outright and
    # take the whole stream down with it.
    assert delivered == [1.0, 0.0]


async def test_an_over_long_transcript_is_bounded_before_it_reaches_the_session() -> None:
    client = FakeTeamONClient([TeamONTranscript(text="я" * 9_000, at_ms=1)])
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    stream = await adapter.events()
    delivered = [raw async for raw in stream]

    assert len(delivered[0].transcript) == MAX_TRANSCRIPT_CHARS


async def test_a_negative_timestamp_is_floored_rather_than_refused() -> None:
    client = FakeTeamONClient([TeamONTranscript(text="стоп", at_ms=-5)])
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    stream = await adapter.events()
    delivered = [raw async for raw in stream]

    assert delivered[0].at_ms == 0


async def test_speaking_passes_urgency_and_interruptibility_to_the_client() -> None:
    client = FakeTeamONClient()
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    await adapter.speak(utterance(OutputKind.STATUS, topic="session", text="Спокойно."))
    await adapter.speak(utterance(OutputKind.STOP, topic="stop", text="Остановился."))

    assert [request.priority for request in client.spoken] == [
        OutputKind.STATUS.rank,
        OutputKind.STOP.rank,
    ]
    assert [request.interruptible for request in client.spoken] == [True, False]
    assert len({request.utterance_id for request in client.spoken}) == 2


async def test_an_interrupted_utterance_is_reported_cancelled_when_the_client_is_silent() -> None:
    client = FakeTeamONClient()
    client.hold = True
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())
    message = utterance(text="Ищу еду.")

    speaking = asyncio.create_task(adapter.speak(message))
    await client.wait_until_speaking()
    await adapter.cancel_speech()

    with pytest.raises(SpeechCancelled):
        await speaking
    assert client.interrupted == ["teamon-1"]


async def test_cancelling_with_nothing_in_flight_does_not_call_the_client() -> None:
    client = FakeTeamONClient()
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    await adapter.cancel_speech()

    assert client.interrupted == []
    assert adapter.current_utterance_id is None


async def test_an_utterance_that_completes_is_not_reported_cancelled() -> None:
    client = FakeTeamONClient()
    adapter = TeamONVoiceAdapter(client, clock=FakeClock())

    await adapter.speak(utterance(text="Готово."))
    await settle()

    assert client.interrupted == []
    assert adapter.current_utterance_id is None


# --------------------------------------------------------------------------
# the vendor SDK, which is not here
# --------------------------------------------------------------------------


async def test_the_sdk_probe_and_the_import_agree() -> None:
    available = teamon_sdk_available()

    if available:
        assert require_teamon_sdk() is not None
        return
    with pytest.raises(TeamONSdkUnavailable) as raised:
        require_teamon_sdk()
    assert TEAMON_SDK_MODULE in str(raised.value)
    # The install step, not a traceback: that is what the user has to act on.
    assert "install" in str(raised.value)


async def test_the_adapter_itself_needs_nothing_installed() -> None:
    # The whole point of the injected client: the plugin is exercised on a
    # machine that has never seen the vendor package.
    adapter = TeamONVoiceAdapter(FakeTeamONClient(), clock=FakeClock())

    await adapter.speak(utterance(text="Готово."))

    assert adapter.current_utterance_id is None
