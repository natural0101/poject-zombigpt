"""The bridge's wire, its bounds and its refusals — pinned, not round-tripped.

A round trip through this module's own encoder and its own decoder proves that
the two halves agree with each other, which they would also do if every key on
the wire were misspelled the same way in both. So the load-bearing shapes here
are pinned against **hand-written byte literals**: the exact line a ``hello`` is,
the exact line a ``speak`` is, the direction table, the version string, the byte
caps, and the Russian sentences the companion says out loud. Change any of them
and a test in this file fails with the literal it expected.

The three properties this file exists to defend, none of which a round trip can
see:

**Bounded.** A line past the cap is dropped *as it arrives*: the assertions
watch :attr:`LineFramer.buffered` while a megabyte goes through it, so an
implementation that accumulated the line and refused it at the newline would
fail even though it also "refuses overlong lines".

**Deadlined.** :meth:`JsonlBridge._take_event` is called with a deadline that
has already passed and with one 50 ms out, and both are required to raise
promptly. The private method is reached directly on purpose: an unbounded wait
is invisible from the outside — the test simply never finishes — and a test that
never finishes is not a failing test, it is a hung suite.

**Quiet.** Everything the far end chooses — a message type name, a status name,
a version string, a fault's free-text detail — is fed in carrying an
unmistakable secret marker, and the refusals and the reports are then searched
for it. A refusal that echoes what it refused is how a transcript, a path or a
key reaches a bug report.

Process-level claims are not here. They are in
``tests/contract/test_teamon_bridge_e2e.py``, against real children over real
pipes, because back-pressure, a refused kill and a bounded restart are claims
about processes and a double cannot be wrong about any of them.
"""

from __future__ import annotations

# Several assertions below compare against sentences from the closed Russian
# phrase table, letter for letter. They are short enough that every character in
# them has a Latin lookalike, so the confusable-character rule reads them as
# mistyped ASCII; taking its suggestion would leave the tests asserting against
# strings this build never says.
# ruff: noqa: RUF001
import io
import queue
import subprocess
import threading
import time
from pathlib import Path, PureWindowsPath

import pytest

from pz_agent_voice.adapters.teamon import TeamONClient
from pz_agent_voice.messages import MAX_TEXT_CHARS, MAX_TRANSCRIPT_CHARS, VoiceGoal
from pz_agent_voice.teamon import (
    # Private: the slice a caller's wait is spent in is an argument to
    # `queue.get`, and an argument is not visible from a return value.
    _LIVENESS_POLL_SECONDS,
    BRIDGE_PROTOCOL_VERSION,
    BRIDGE_UNAVAILABLE_PHRASE,
    CREATE_NO_WINDOW,
    FAULT_PHRASES,
    MAX_LINE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_TIMEOUT_SECONDS,
    MESSAGE_DIRECTIONS,
    BridgeCheck,
    BridgeConfig,
    BridgeDirection,
    BridgeFailed,
    BridgeFault,
    BridgeFaultCode,
    BridgeMessage,
    BridgeMessageType,
    BridgeOutcome,
    BridgeReason,
    BridgeReport,
    BridgeState,
    BridgeTimeout,
    BridgeTranscript,
    BridgeUnavailable,
    JsonlBridge,
    LineFramer,
    MalformedMessage,
    MessageTooLarge,
    OutcomeStatus,
    OverlongLine,
    ProtocolMismatch,
    TeamONBridgeClient,
    UnknownMessageType,
    # Private: the clamp it applies is not reachable through any public call
    # that does not also wait for the result of it.
    _deadline,
    check_bridge,
    check_protocol_version,
    creation_flags,
    decode,
    encode,
    goal_message,
    hello_message,
    interrupt_message,
    parse_version,
    program_is_path,
    read_error,
    read_outcome,
    read_ready,
    read_transcript,
    speak_message,
)

#: Planted in everything the far end is allowed to choose. If it ever comes back
#: out of a refusal or a report, the refusal quoted its input.
SECRET = "sk-live-ZOMBI-31337"


def a_config(**overrides: object) -> BridgeConfig:
    """A configuration that never launches anything; the command is unused."""
    fields: dict[str, object] = {"command": ("pz-teamon-bridge",)}
    fields.update(overrides)
    return BridgeConfig(**fields)  # type: ignore[arg-type]


class Recorder:
    """A listener that keeps every report, so refusals can be searched."""

    def __init__(self) -> None:
        self.reports: list[BridgeReport] = []

    def __call__(self, report: BridgeReport) -> None:
        self.reports.append(report)

    @property
    def details(self) -> str:
        return "\n".join(report.detail for report in self.reports)

    def reasons(self) -> list[BridgeReason]:
        return [report.reason for report in self.reports]


# ---------------------------------------------------------------------------
# the constants, against hand-written literals
# ---------------------------------------------------------------------------


def test_the_wire_constants_are_the_ones_the_bridge_program_was_built_against() -> None:
    assert BRIDGE_PROTOCOL_VERSION == "1.0"
    assert MAX_MESSAGE_BYTES == 16384
    assert MAX_LINE_BYTES == 16384
    assert MAX_TIMEOUT_SECONDS == 60.0
    # A message that this side may send but the other side must refuse to read
    # would be a protocol that cannot carry its own longest legal message.
    assert MAX_LINE_BYTES >= MAX_MESSAGE_BYTES


def test_every_message_type_travels_in_exactly_the_declared_direction() -> None:
    # Hand-written, string for string. A swapped direction would let a `speak`
    # arriving *from* the bridge be accepted as a command this side issued.
    assert {str(kind): str(way) for kind, way in MESSAGE_DIRECTIONS.items()} == {
        "hello": "to_bridge",
        "speak": "to_bridge",
        "interrupt": "to_bridge",
        "goal": "to_bridge",
        "ready": "from_bridge",
        "transcript": "from_bridge",
        "outcome": "from_bridge",
        "error": "from_bridge",
    }


def test_the_message_types_are_exactly_these_eight() -> None:
    assert sorted(member.value for member in BridgeMessageType) == [
        "error",
        "goal",
        "hello",
        "interrupt",
        "outcome",
        "ready",
        "speak",
        "transcript",
    ]


def test_the_outcome_statuses_are_exactly_these_four_and_only_one_claims_success() -> None:
    assert sorted(member.value for member in OutcomeStatus) == [
        "cancelled",
        "ended",
        "failed",
        "refused",
    ]
    ended = [
        status
        for status in OutcomeStatus
        if BridgeOutcome(request_id="teamon-1", status=status).ended
    ]
    assert ended == [OutcomeStatus.ENDED]


def test_every_fault_has_the_sentence_the_companion_is_allowed_to_say() -> None:
    # Pinned letter for letter: these reach a synthesiser, and a wrong one is a
    # sentence a user hears in a language this build claims to speak.
    assert {str(code): phrase for code, phrase in FAULT_PHRASES.items()} == {
        "audio_device": "Микрофон недоступен.",
        "recogniser": "Распознавание не работает.",
        "synthesis": "Не могу говорить.",
        "network": "Голосовой сервис не отвечает.",
        "internal": "Ошибка голосового моста.",
        "unknown": "Ошибка голосового моста.",
    }
    assert BRIDGE_UNAVAILABLE_PHRASE == "Голосовой мост не работает."


# ---------------------------------------------------------------------------
# encoding, against hand-written lines
# ---------------------------------------------------------------------------


def test_a_hello_is_exactly_this_line() -> None:
    assert encode(hello_message()) == b'{"v":"1.0","type":"hello"}\n'


def test_a_speak_is_exactly_this_line() -> None:
    message = speak_message(
        utterance_id="teamon-7",
        text="Готово.",
        priority=0,
        interruptible=False,
    )
    assert encode(message) == (
        b'{"v":"1.0","type":"speak","utterance_id":"teamon-7","text":"\xd0\x93\xd0\xbe\xd1\x82'
        b'\xd0\xbe\xd0\xb2\xd0\xbe.","priority":0,"interruptible":false}\n'
    )


def test_an_interrupt_is_exactly_this_line() -> None:
    assert encode(interrupt_message("teamon-7")) == (
        b'{"v":"1.0","type":"interrupt","utterance_id":"teamon-7"}\n'
    )


def test_a_goal_is_exactly_this_line_and_carries_a_token_not_a_transcript() -> None:
    assert encode(goal_message(request_id="teamon-2", goal=VoiceGoal.DRINK)) == (
        b'{"v":"1.0","type":"goal","request_id":"teamon-2","goal":"drink"}\n'
    )


def test_a_goal_can_only_be_one_of_the_four_tokens() -> None:
    lines = {goal: encode(goal_message(request_id="teamon-1", goal=goal)) for goal in VoiceGoal}
    assert sorted(str(goal) for goal in lines) == ["drink", "eat", "read", "resume"]


def test_every_message_carries_the_version_and_ends_with_exactly_one_newline() -> None:
    for message in (
        hello_message(),
        speak_message(utterance_id="teamon-1", text="Да.", priority=1, interruptible=True),
        interrupt_message("teamon-1"),
        goal_message(request_id="teamon-1", goal=VoiceGoal.EAT),
    ):
        line = encode(message)
        assert line.count(b"\n") == 1
        assert line.endswith(b"\n")
        assert b'"v":"1.0"' in line


def test_a_newline_inside_an_utterance_is_escaped_and_never_ends_the_line() -> None:
    # The one way a message could span two lines. `json.dumps` escapes it, and
    # the framer must therefore see a single line that decodes whole.
    message = speak_message(
        utterance_id="teamon-1",
        text='first\nsecond\r\n{"v":"1.0","type":"ready"}',
        priority=1,
        interruptible=True,
    )
    line = encode(message)
    assert line.count(b"\n") == 1
    assert b"\\n" in line
    framed = LineFramer().feed(line)
    assert len(framed) == 1
    assert isinstance(framed[0], bytes)
    round_tripped = decode(framed[0], expect=BridgeDirection.TO_BRIDGE)
    assert round_tripped.fields["text"] == 'first\nsecond\r\n{"v":"1.0","type":"ready"}'


def test_an_utterance_past_the_character_cap_is_refused_rather_than_truncated() -> None:
    with pytest.raises(MalformedMessage):
        speak_message(
            utterance_id="teamon-1",
            text="а" * (MAX_TEXT_CHARS + 1),
            priority=0,
            interruptible=True,
        )


def test_an_empty_utterance_and_a_negative_priority_are_refused() -> None:
    with pytest.raises(MalformedMessage):
        speak_message(utterance_id="teamon-1", text="   ", priority=0, interruptible=True)
    with pytest.raises(MalformedMessage):
        speak_message(utterance_id="teamon-1", text="Да.", priority=-1, interruptible=True)


def test_a_handle_that_is_not_a_handle_is_refused_and_never_quoted_back() -> None:
    for bad in (f"../../{SECRET}", "Teamon-1", "", "x" * 65, 'a","injected":"'):
        with pytest.raises(MalformedMessage) as caught:
            interrupt_message(bad)
        assert SECRET not in str(caught.value)
        assert bad not in str(caught.value) or bad == ""


def test_a_message_past_the_byte_cap_is_refused_rather_than_sent() -> None:
    # Built past the cap deliberately, bypassing `speak_message`'s character
    # limit, because the byte cap is the one the far end's reader enforces.
    huge = BridgeMessage(
        type=BridgeMessageType.SPEAK, fields={"text": "a" * (MAX_MESSAGE_BYTES + 1)}
    )
    with pytest.raises(MessageTooLarge):
        encode(huge)


def test_a_field_that_is_not_json_is_refused_without_naming_its_value() -> None:
    with pytest.raises(MalformedMessage) as caught:
        encode(BridgeMessage(type=BridgeMessageType.SPEAK, fields={"text": {SECRET}}))
    assert SECRET not in str(caught.value)


# ---------------------------------------------------------------------------
# decoding, against hand-written lines
# ---------------------------------------------------------------------------


def test_a_ready_line_is_read_as_the_version_the_bridge_declared() -> None:
    message = decode(b'{"v":"1.0","type":"ready"}')
    assert message.type is BridgeMessageType.READY
    assert read_ready(message) == (1, 0)


def test_a_transcript_line_is_read_field_for_field() -> None:
    message = decode(
        b'{"v":"1.0","type":"transcript","text":"\xd1\x81\xd1\x82\xd0\xbe\xd0\xbf",'
        b'"at_ms":1234,"final":false,"confidence":0.25}'
    )
    assert read_transcript(message) == BridgeTranscript(
        text="стоп", at_ms=1234, final=False, confidence=0.25
    )


def test_a_transcript_omitting_the_optional_fields_is_final_and_certain() -> None:
    message = decode(b'{"v":"1.0","type":"transcript","text":"\xd0\xb4\xd0\xb0","at_ms":0}')
    assert read_transcript(message) == BridgeTranscript(
        text="да", at_ms=0, final=True, confidence=1.0
    )


def test_a_transcript_is_clamped_and_bounded_rather_than_trusted() -> None:
    message = decode(
        b'{"v":"1.0","type":"transcript","text":"' + b"a" * 900 + b'","at_ms":-5,"confidence":1.7}'
    )
    transcript = read_transcript(message)
    assert len(transcript.text) == MAX_TRANSCRIPT_CHARS
    assert transcript.at_ms == 0
    assert transcript.confidence == 1.0
    low = decode(b'{"v":"1.0","type":"transcript","text":"x","at_ms":1,"confidence":-4}')
    assert read_transcript(low).confidence == 0.0


def test_a_boolean_timestamp_is_refused_because_true_is_not_one_millisecond() -> None:
    message = decode(b'{"v":"1.0","type":"transcript","text":"x","at_ms":true}')
    with pytest.raises(MalformedMessage):
        read_transcript(message)


def test_an_outcome_line_is_read_as_the_request_it_names() -> None:
    message = decode(b'{"v":"1.0","type":"outcome","request_id":"teamon-3","status":"ended"}')
    outcome = read_outcome(message)
    assert outcome == BridgeOutcome(request_id="teamon-3", status=OutcomeStatus.ENDED)
    assert outcome.ended is True


def test_an_outcome_that_is_not_ended_never_claims_the_thing_happened() -> None:
    for status in ("failed", "refused", "cancelled"):
        line = f'{{"v":"1.0","type":"outcome","request_id":"teamon-3","status":"{status}"}}'
        assert read_outcome(decode(line.encode("utf-8"))).ended is False


def test_an_unreadable_status_is_refused_and_never_quoted_back() -> None:
    line = f'{{"v":"1.0","type":"outcome","request_id":"teamon-3","status":"{SECRET}"}}'
    with pytest.raises(MalformedMessage) as caught:
        read_outcome(decode(line.encode("utf-8")))
    assert SECRET not in str(caught.value)
    assert "ended" in str(caught.value)


def test_an_error_is_classified_and_its_free_text_is_dropped_on_the_floor() -> None:
    line = f'{{"v":"1.0","type":"error","code":"audio_device","detail":"{SECRET}"}}'
    fault = read_error(decode(line.encode("utf-8")))
    assert fault == BridgeFault(code=BridgeFaultCode.AUDIO_DEVICE)
    assert fault.summary() == "Микрофон недоступен."
    failure = BridgeFailed(fault)
    assert str(failure) == "Микрофон недоступен."
    assert SECRET not in str(failure)
    assert SECRET not in repr(fault)


def test_an_error_code_from_a_newer_bridge_is_still_reported_as_a_failure() -> None:
    line = f'{{"v":"1.0","type":"error","code":"{SECRET}"}}'
    assert read_error(decode(line.encode("utf-8"))).code is BridgeFaultCode.UNKNOWN
    assert read_error(decode(b'{"v":"1.0","type":"error"}')).code is BridgeFaultCode.UNKNOWN


def test_a_line_that_is_not_json_is_refused_without_repeating_it() -> None:
    with pytest.raises(MalformedMessage) as caught:
        decode(f"not json at all {SECRET}".encode())
    assert SECRET not in str(caught.value)


def test_a_line_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(MalformedMessage):
        decode(b'{"v":"1.0","type":"ready","x":"\xff\xfe"}')


def test_a_json_scalar_is_not_a_message() -> None:
    with pytest.raises(MalformedMessage):
        decode(b'"ready"')
    with pytest.raises(MalformedMessage):
        decode(b"[1,2,3]")


def test_a_message_with_no_version_is_refused() -> None:
    with pytest.raises(MalformedMessage):
        decode(b'{"type":"ready"}')
    with pytest.raises(MalformedMessage):
        decode(b'{"v":10,"type":"ready"}')


def test_an_unknown_type_is_refused_and_the_name_it_used_is_not_echoed() -> None:
    with pytest.raises(UnknownMessageType) as caught:
        decode(f'{{"v":"1.0","type":"{SECRET}"}}'.encode())
    assert SECRET not in str(caught.value)
    # The known set is this build's own, so listing it leaks nothing and is the
    # only thing an integrator can act on.
    assert "transcript" in str(caught.value)


def test_a_command_arriving_from_the_bridge_is_refused_as_firmly_as_a_bad_type() -> None:
    # `speak` travels to_bridge. Reading one back means the far end is not the
    # bridge, and accepting it would let it drive this side's own command path.
    with pytest.raises(UnknownMessageType):
        decode(b'{"v":"1.0","type":"speak","utterance_id":"teamon-1"}')
    assert (
        decode(
            b'{"v":"1.0","type":"speak","utterance_id":"teamon-1"}',
            expect=BridgeDirection.TO_BRIDGE,
        ).type
        is BridgeMessageType.SPEAK
    )


def test_a_line_past_the_cap_is_refused_by_the_decoder_too() -> None:
    with pytest.raises(MessageTooLarge):
        decode(b"x" * (MAX_LINE_BYTES + 1))


def test_reading_the_wrong_message_type_is_a_refusal_not_a_guess() -> None:
    ready = decode(b'{"v":"1.0","type":"ready"}')
    with pytest.raises(MalformedMessage):
        read_transcript(ready)
    with pytest.raises(MalformedMessage):
        read_outcome(ready)
    with pytest.raises(MalformedMessage):
        read_error(ready)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_a_minor_difference_is_compatible_and_a_major_one_is_not() -> None:
    assert check_protocol_version("1.0") == (1, 0)
    assert check_protocol_version("1.940") == (1, 940)
    with pytest.raises(ProtocolMismatch) as caught:
        check_protocol_version("2.3")
    # The numbers come back out of this module's own regex groups, so quoting
    # them is quoting integers, and the integrator needs both to act.
    assert "2.3" in str(caught.value)
    assert "1.0" in str(caught.value)


def test_a_version_that_is_not_a_version_is_refused_without_being_quoted() -> None:
    for bad in (SECRET, "1", "1.2.3", "v1.0", "", "1234.0", "1.99999"):
        with pytest.raises(MalformedMessage) as caught:
            parse_version(bad)
        assert SECRET not in str(caught.value)
        assert bad not in str(caught.value) or bad == ""


def test_a_ready_from_a_future_major_is_a_mismatch_at_decode_time() -> None:
    with pytest.raises(ProtocolMismatch):
        decode(b'{"v":"9.0","type":"ready"}')


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def test_two_messages_in_one_chunk_are_two_lines() -> None:
    framer = LineFramer()
    lines = framer.feed(b'{"v":"1.0","type":"ready"}\n{"v":"1.0","type":"error"}\n')
    assert lines == [b'{"v":"1.0","type":"ready"}', b'{"v":"1.0","type":"error"}']


def test_a_message_split_byte_by_byte_reassembles_into_exactly_one_line() -> None:
    payload = encode(hello_message())
    framer = LineFramer()
    collected: list[bytes | OverlongLine] = []
    for index in range(len(payload)):
        collected.extend(framer.feed(payload[index : index + 1]))
    assert collected == [b'{"v":"1.0","type":"hello"}']


def test_a_windows_child_writing_crlf_still_produces_a_readable_line() -> None:
    # The Windows shape, written as bytes rather than inferred: a console child
    # with a text-mode stdout emits \r\n, and the \r would otherwise be inside
    # the JSON the decoder sees.
    framer = LineFramer()
    lines = framer.feed(b'{"v":"1.0","type":"ready"}\r\n')
    assert lines == [b'{"v":"1.0","type":"ready"}']
    assert read_ready(decode(b'{"v":"1.0","type":"ready"}')) == (1, 0)


def test_exactly_one_carriage_return_is_stripped_and_an_interior_one_is_kept() -> None:
    # Exactly one, because that is the number a Windows text-mode stdout adds.
    # Stripping every trailing \r would be the framer silently editing content
    # the far end chose, and stripping none would break the shipping platform.
    assert LineFramer().feed(b'{"v":"1.0","type":"ready"}\r\r\n') == [
        b'{"v":"1.0","type":"ready"}\r'
    ]
    assert LineFramer().feed(b"a\rb\n") == [b"a\rb"]


def test_an_overlong_line_is_dropped_as_it_arrives_and_never_buffered() -> None:
    framer = LineFramer(limit=64)
    peak = 0
    for _ in range(1024):
        framer.feed(b"x" * 1024)
        peak = max(peak, framer.buffered)
    # The point of the whole design: an implementation that accumulated the line
    # and refused it at the newline would also "refuse overlong lines", and
    # would hold a megabyte here.
    assert peak <= 64
    lines = framer.feed(b"\n")
    assert lines == [OverlongLine(dropped_bytes=1024 * 1024)]


def test_the_line_after_an_overlong_one_still_parses() -> None:
    framer = LineFramer(limit=32)
    lines = framer.feed(b"y" * 200 + b"\n" + b'{"v":"1.0","type":"ready"}' + b"\n")
    assert lines == [OverlongLine(dropped_bytes=200), b'{"v":"1.0","type":"ready"}']
    tail = lines[1]
    assert isinstance(tail, bytes)
    assert decode(tail).type is BridgeMessageType.READY


def test_a_line_of_exactly_the_limit_is_kept_and_one_byte_more_is_not() -> None:
    at_limit = LineFramer(limit=8).feed(b"12345678\n")
    assert at_limit == [b"12345678"]
    over = LineFramer(limit=8).feed(b"123456789\n")
    assert over == [OverlongLine(dropped_bytes=9)]


def test_a_framer_with_a_useless_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        LineFramer(limit=0)


def test_the_default_framer_limit_is_the_wire_cap() -> None:
    assert LineFramer().limit == MAX_LINE_BYTES


# ---------------------------------------------------------------------------
# configuration, and the absence of credentials
# ---------------------------------------------------------------------------


def test_a_configuration_with_no_command_is_refused() -> None:
    with pytest.raises(ValueError, match="command"):
        BridgeConfig(command=())
    with pytest.raises(ValueError, match="empty argument"):
        BridgeConfig(command=("bridge", ""))


def test_a_credential_shaped_option_is_refused_and_its_value_is_not_repeated() -> None:
    for option in (f"--api-key={SECRET}", f"--token={SECRET}", f"--Auth-Secret={SECRET}"):
        with pytest.raises(ValueError) as caught:
            BridgeConfig(command=("bridge", option))
        assert SECRET not in str(caught.value)
        assert "credential" in str(caught.value)


def test_a_program_path_that_merely_contains_an_unlucky_word_still_launches() -> None:
    # A check that refuses `C:\Users\monkey\keystore\bridge.exe` is a check that
    # gets switched off rather than obeyed.
    config = BridgeConfig(command=(r"C:\Users\a\keys\authority\bridge.exe", "--quiet"))
    assert config.command[0].endswith("bridge.exe")


def test_a_credential_shaped_environment_name_is_refused() -> None:
    with pytest.raises(ValueError, match="credential"):
        BridgeConfig(command=("bridge",), env={"TEAMON_API_KEY": SECRET})
    with pytest.raises(ValueError, match="environment variable name"):
        BridgeConfig(command=("bridge",), env={"not a name": "x"})


def test_a_credential_shaped_configuration_key_is_refused_by_name() -> None:
    with pytest.raises(ValueError) as caught:
        BridgeConfig.from_mapping({"command": ["bridge"], "api_token": SECRET})
    assert SECRET not in str(caught.value)
    assert "api_token" in str(caught.value)


def test_an_unknown_configuration_key_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError) as caught:
        BridgeConfig.from_mapping({"command": ["bridge"], "startup_timout": 9})
    assert "startup_timeout" in str(caught.value)


def test_a_configuration_loads_from_a_mapping_with_every_bound_named() -> None:
    config = BridgeConfig.from_mapping(
        {
            "command": ["python", "-u", "bridge.py"],
            "env": {"TEAMON_DEVICE": "default"},
            "startup_timeout": 2,
            "reply_timeout": 3,
            "speech_timeout": 4,
            "stop_timeout": 0.5,
            "max_restarts": 1,
            "max_pending": 8,
        }
    )
    assert config.command == ("python", "-u", "bridge.py")
    assert dict(config.env) == {"TEAMON_DEVICE": "default"}
    assert (config.startup_timeout, config.reply_timeout) == (2.0, 3.0)
    assert (config.speech_timeout, config.stop_timeout) == (4.0, 0.5)
    assert (config.max_restarts, config.max_pending) == (1, 8)


def test_the_defaults_are_the_ones_the_bounds_were_chosen_to_be() -> None:
    config = a_config()
    assert config.startup_timeout == 5.0
    assert config.reply_timeout == 5.0
    assert config.speech_timeout == 30.0
    assert config.stop_timeout == 1.0
    assert config.max_restarts == 3
    assert config.max_pending == 64


def test_a_command_that_is_not_a_list_of_strings_is_refused() -> None:
    for bad in ({"command": "bridge"}, {"command": [1, 2]}, {}):
        with pytest.raises(ValueError, match="command"):
            BridgeConfig.from_mapping(bad)


def test_a_timeout_that_is_not_a_bound_is_refused() -> None:
    for value in (0, -1, MAX_TIMEOUT_SECONDS + 0.1, 3600):
        with pytest.raises(ValueError, match="seconds"):
            a_config(startup_timeout=value)
    with pytest.raises(ValueError, match="seconds"):
        a_config(stop_timeout=0)


def test_a_restart_count_and_a_queue_depth_are_bounded_in_both_directions() -> None:
    for restarts in (-1, 11):
        with pytest.raises(ValueError, match="max_restarts"):
            a_config(max_restarts=restarts)
    for depth in (0, 1025):
        with pytest.raises(ValueError, match="max_pending"):
            a_config(max_pending=depth)
    assert a_config(max_restarts=0).max_restarts == 0


def test_a_boolean_is_not_a_number_in_a_configuration() -> None:
    with pytest.raises(ValueError, match="number"):
        BridgeConfig.from_mapping({"command": ["bridge"], "max_pending": True})


def test_the_child_gets_an_allowlist_and_not_this_process_s_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PZ_AGENT_PLANNER_API_KEY", SECRET)
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = a_config(env={"TEAMON_DEVICE": "default"}).child_environment()
    assert environment["PATH"] == "/usr/bin"
    assert environment["TEAMON_DEVICE"] == "default"
    assert "PZ_AGENT_PLANNER_API_KEY" not in environment
    assert SECRET not in "".join(environment.values())


def test_the_allowlist_carries_the_names_a_windows_child_cannot_start_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Windows process with no SYSTEMROOT cannot load ws2_32.dll, so a bridge
    # that opens a socket dies on launch with nothing useful to say.
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    environment = a_config().child_environment()
    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["COMSPEC"] == r"C:\Windows\system32\cmd.exe"


# ---------------------------------------------------------------------------
# the Windows shapes, constructed on Linux
# ---------------------------------------------------------------------------


def test_a_windows_launch_asks_for_no_console_window_and_every_other_asks_for_nothing() -> None:
    assert CREATE_NO_WINDOW == 0x08000000
    assert creation_flags("win32") == 0x08000000
    assert creation_flags("linux") == 0
    assert creation_flags("darwin") == 0
    assert creation_flags("freebsd13") == 0


@pytest.mark.skipif(
    not hasattr(subprocess, "CREATE_NO_WINDOW"),
    reason="subprocess.CREATE_NO_WINDOW exists only on Windows",
)
def test_the_flag_is_the_one_cpython_defines() -> None:
    # Reached through `getattr` because the name does not exist off Windows and
    # this module is type-checked on Linux, where referring to it is an error.
    cpython_flag: int = getattr(subprocess, "CREATE_NO_WINDOW")  # noqa: B009
    assert cpython_flag == CREATE_NO_WINDOW


def test_a_windows_program_location_is_recognised_in_every_spelling_windows_has() -> None:
    assert program_is_path(r"C:\Program Files\TeamON\bridge.exe") is True
    assert program_is_path("C:/Program Files/TeamON/bridge.exe") is True
    assert program_is_path(r"teamon\bridge.exe") is True
    assert program_is_path(r"\\server\share\bridge.exe") is True
    # Drive-relative: a real Windows path with no separator in it at all. A
    # naive "is there a slash" test reads this as a bare name to find on PATH.
    assert program_is_path("C:bridge.exe") is True
    assert program_is_path(str(PureWindowsPath("C:/tools/bridge.exe"))) is True
    assert program_is_path("/usr/local/bin/pz-teamon-bridge") is True
    assert program_is_path("bridge.exe") is False
    assert program_is_path("pz-teamon-bridge") is False


def test_a_windows_launch_attempted_off_windows_fails_as_a_launch_and_leaks_nothing() -> None:
    # Proof that the flag actually reaches Popen rather than being computed and
    # dropped: off Windows, CPython refuses a non-zero `creationflags`, so an
    # injected win32 platform must surface as a launch failure — with a state
    # that is STOPPED rather than STARTING, and a detail with no path in it.
    recorder = Recorder()
    bridge = JsonlBridge(a_config(command=("/bin/true",)), listener=recorder, system="win32")
    with pytest.raises(BridgeUnavailable) as caught:
        bridge.start()
    assert "/bin/true" not in str(caught.value)
    assert bridge.state is BridgeState.STOPPED
    assert bridge.pid is None
    assert recorder.reasons() == [BridgeReason.LAUNCH_FAILED]
    bridge.stop()


def test_a_program_that_is_not_there_is_a_launch_failure_that_names_no_path() -> None:
    recorder = Recorder()
    secret_path = f"/nonexistent/{SECRET}/bridge"
    bridge = JsonlBridge(a_config(command=(secret_path,)), listener=recorder)
    with pytest.raises(BridgeUnavailable) as caught:
        bridge.start()
    assert SECRET not in str(caught.value)
    assert SECRET not in recorder.details
    assert "command" in str(caught.value)
    bridge.stop()


# ---------------------------------------------------------------------------
# check_bridge
# ---------------------------------------------------------------------------


def test_an_unconfigured_bridge_is_a_fact_and_not_an_exception() -> None:
    check = check_bridge(None)
    assert check == BridgeCheck(
        configured=False,
        available=False,
        resolved="",
        detail="the voice bridge is not configured; voice runs without it",
    )


def test_a_missing_bridge_program_is_reported_without_naming_the_path() -> None:
    check = check_bridge(a_config(command=(f"/nonexistent/{SECRET}/bridge",)))
    assert check.configured is True
    assert check.available is False
    assert SECRET not in check.detail
    assert check.resolved == ""


def test_a_bridge_program_that_exists_as_a_file_is_found_even_off_path(tmp_path: Path) -> None:
    program = tmp_path / "bridge.py"
    program.write_text("print('hi')\n", encoding="utf-8")
    check = check_bridge(a_config(command=(str(program),)))
    assert check.available is True
    assert check.resolved == str(program)
    # The path is a structured field, never the sentence that reaches a log.
    assert str(program) not in check.detail


# ---------------------------------------------------------------------------
# deadlines
# ---------------------------------------------------------------------------


def test_a_silent_bridge_times_out_instead_of_waiting_forever() -> None:
    # The whole reason this reaches a private method: an unbounded wait is
    # invisible from outside — the test would simply never return, and a suite
    # that hangs is not a suite that fails.
    bridge = JsonlBridge(a_config())
    started = time.monotonic()
    with pytest.raises(BridgeTimeout):
        bridge._take_event(time.monotonic() + 0.05)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0


def test_a_deadline_that_has_already_passed_is_not_waited_on_at_all() -> None:
    bridge = JsonlBridge(a_config())
    started = time.monotonic()
    with pytest.raises(BridgeTimeout):
        bridge._take_event(time.monotonic() - 1.0)
    assert time.monotonic() - started < 0.5


def test_the_timeout_message_names_the_bridge_and_quotes_nothing() -> None:
    bridge = JsonlBridge(a_config())
    with pytest.raises(BridgeTimeout) as caught:
        bridge._take_event(time.monotonic() - 1.0)
    assert "voice bridge" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_next_transcript_is_capped_at_the_module_ceiling_even_if_asked_for_more() -> None:
    # A caller passing an hour must not get an hour. The call is on a stopped
    # bridge, so it refuses before the wait; the ceiling itself is asserted by
    # the configuration bound, and this pins that the argument is not trusted.
    bridge = JsonlBridge(a_config())
    with pytest.raises(BridgeUnavailable):
        bridge.next_transcript(3600.0)


# ---------------------------------------------------------------------------
# the state machine, without a process
# ---------------------------------------------------------------------------


def test_a_bridge_that_never_started_refuses_to_send_rather_than_waiting() -> None:
    bridge = JsonlBridge(a_config())
    assert bridge.state is BridgeState.STOPPED
    assert bridge.alive is False
    assert bridge.pid is None
    with pytest.raises(BridgeUnavailable, match="not running"):
        bridge.send(hello_message())


def test_stopping_a_bridge_that_never_started_is_safe_and_repeatable() -> None:
    bridge = JsonlBridge(a_config())
    bridge.stop()
    bridge.stop()
    assert bridge.state is BridgeState.STOPPED


def test_a_dead_bridge_stays_dead_and_says_why_every_time_it_is_asked() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    bridge._die(BridgeReason.RESTART_LIMIT, "the voice bridge stopped too many times")
    assert bridge.state is BridgeState.DEAD
    for _ in range(3):
        with pytest.raises(BridgeUnavailable, match="too many times"):
            bridge.start()
        with pytest.raises(BridgeUnavailable, match="too many times"):
            bridge.send(hello_message())
    # Stopping a dead bridge must not make it look merely stopped: the
    # difference is "it will come back" versus "voice is off for this run".
    bridge.stop()
    assert bridge.state is BridgeState.DEAD


def test_the_death_of_a_bridge_is_a_sentence_the_user_hears() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    bridge._die(BridgeReason.RESTART_LIMIT, "restarted as often as it may be")
    spoken = [report.spoken() for report in recorder.reports]
    assert spoken == [BRIDGE_UNAVAILABLE_PHRASE]


def test_a_routine_report_is_for_the_log_and_not_for_the_loudspeaker() -> None:
    quiet = BridgeReport(
        state=BridgeState.RUNNING,
        reason=BridgeReason.MALFORMED_LINE,
        detail="a line from the voice bridge was refused",
    )
    assert quiet.spoken() is None
    loud = BridgeReport(
        state=BridgeState.RUNNING,
        reason=BridgeReason.FAULT,
        detail="a fault",
        fault=BridgeFault(code=BridgeFaultCode.NETWORK),
    )
    assert loud.spoken() == "Голосовой сервис не отвечает."


def test_a_listener_that_raises_does_not_take_the_reader_thread_with_it() -> None:
    def explode(report: BridgeReport) -> None:
        raise RuntimeError("the log backend is down")

    bridge = JsonlBridge(a_config(), listener=explode)
    # Must not raise: this is the path a *failing* bridge reports through, and
    # losing the report is smaller than losing every transcript after it.
    bridge._report(BridgeReport(state=BridgeState.RUNNING, reason=BridgeReason.DROPPED, detail="x"))


# ---------------------------------------------------------------------------
# the bounded queues
# ---------------------------------------------------------------------------


def a_transcript_line(text: str) -> bytes:
    return f'{{"v":"1.0","type":"transcript","text":"{text}","at_ms":1}}'.encode()


def test_a_full_transcript_queue_drops_the_oldest_and_keeps_the_newest() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(max_pending=2), listener=recorder)
    generation = bridge._generation
    for text in ("one", "two", "three"):
        bridge._admit(a_transcript_line(text), generation)
    kept = [bridge._transcripts.get_nowait().text for _ in range(2)]
    # A transcript is only useful while it is fresh, and the newest is the one
    # the user is waiting on.
    assert kept == ["two", "three"]
    assert BridgeReason.DROPPED in recorder.reasons()


def test_a_transcript_from_a_previous_child_never_reaches_the_next_session() -> None:
    bridge = JsonlBridge(a_config())
    stale = bridge._generation
    bridge._generation += 1
    bridge._admit(a_transcript_line("ghost"), stale)
    assert bridge._transcripts.empty()


def test_a_malformed_line_is_reported_and_skipped_rather_than_fatal() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    generation = bridge._generation
    bridge._admit(b"", generation)
    bridge._admit(b"   ", generation)
    bridge._admit(f"garbage {SECRET}".encode(), generation)
    bridge._admit(f'{{"v":"1.0","type":"{SECRET}"}}'.encode(), generation)
    bridge._admit(b'{"v":"1.0","type":"transcript","text":5,"at_ms":1}', generation)
    # And then a good one, which must still land: that is the recovery the whole
    # read loop depends on.
    bridge._admit(a_transcript_line("still here"), generation)
    assert bridge._transcripts.get_nowait().text == "still here"
    assert recorder.reasons() == [
        BridgeReason.MALFORMED_LINE,
        BridgeReason.UNKNOWN_TYPE,
        BridgeReason.MALFORMED_LINE,
    ]
    assert SECRET not in recorder.details


def test_an_overlong_line_is_reported_with_its_size_and_none_of_its_content() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    bridge._admit(OverlongLine(dropped_bytes=99_999), bridge._generation)
    assert recorder.reasons() == [BridgeReason.OVERLONG_LINE]
    assert "99999" in recorder.details
    assert recorder.reports[0].spoken() is None


def test_a_version_mismatch_kills_the_bridge_rather_than_restarting_it() -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    generation = bridge._generation
    bridge._admit(b'{"v":"9.4","type":"ready"}', generation)
    assert recorder.reasons() == [BridgeReason.VERSION_MISMATCH]
    with pytest.raises(ProtocolMismatch):
        bridge._handle_down(bridge._events.get_nowait())  # type: ignore[arg-type]
    assert bridge.state is BridgeState.DEAD
    # No restart fixes a bridge built for another protocol, so it must not be
    # counted as a crash and tried again.
    assert bridge.restarts == 0


# ---------------------------------------------------------------------------
# the client's surface
# ---------------------------------------------------------------------------


def test_the_client_is_the_adapter_s_transport_and_mypy_is_the_one_checking() -> None:
    client = TeamONBridgeClient(a_config())
    # Not an isinstance check — `TeamONClient` is not runtime-checkable. This
    # line is verified by mypy in strict mode, which is the point: the bridge
    # client satisfies the protocol the adapter is written against, or the type
    # check fails.
    transport: TeamONClient = client
    assert transport is client
    assert client.state is BridgeState.STOPPED
    assert isinstance(client.bridge, JsonlBridge)


def test_the_client_exposes_the_supervisor_it_wraps() -> None:
    config = a_config(max_pending=4)
    client = TeamONBridgeClient(config)
    assert client.bridge.state is BridgeState.STOPPED
    assert client.bridge._config is config


# ---------------------------------------------------------------------------
# the bounds that were computed and never observed
# ---------------------------------------------------------------------------
#
# Each test below was written because deleting the line it is about left the
# whole suite green. `min(timeout, MAX_TIMEOUT_SECONDS)` in `next_transcript`,
# the same clamp in `_deadline`, and the `timeout=` on the joins in `stop` were
# all unobserved: the arguments those calls are handed are not visible from
# outside, and the tests that named them asserted something else — the stopped
# bridge refuses before the wait, so a `next_transcript` that clamped nothing
# passed that test exactly as well.


class _RecordsTheTimeoutItWasGiven:
    """A transcript queue that answers nothing and remembers what it was asked.

    The clamp is an argument to `queue.get`, and an argument is not observable
    from a return value. Waiting the un-clamped hour to find out is not an
    option in a suite, so the argument itself is what gets asserted.
    """

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def get(self, timeout: float | None = None) -> BridgeTranscript:
        self.timeouts.append(timeout)
        raise queue.Empty


class _AlwaysAlive:
    """A child that exists and has not exited. Nothing here launches one."""

    pid = 4242

    def poll(self) -> int | None:
        return None


def _running(bridge: JsonlBridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put *bridge* in the one state where a wait is actually reached."""
    monkeypatch.setattr(bridge, "_state", BridgeState.RUNNING)
    monkeypatch.setattr(bridge, "_process", _AlwaysAlive())


def test_next_transcript_hands_the_queue_a_wait_no_longer_than_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = JsonlBridge(a_config())
    _running(bridge, monkeypatch)
    recorder = _RecordsTheTimeoutItWasGiven()
    monkeypatch.setattr(bridge, "_transcripts", recorder)

    assert bridge.next_transcript(3600.0) is None
    assert bridge.next_transcript(0.25) is None
    assert bridge.next_transcript(-5.0) is None

    # An hour becomes the ceiling, a sane wait is passed through untouched, and
    # a negative one becomes zero rather than an immediate exception.
    assert recorder.timeouts == [MAX_TIMEOUT_SECONDS, 0.25, 0.0]


def test_a_deadline_is_capped_at_the_ceiling_however_long_it_is_asked_for() -> None:
    started = time.monotonic()
    far = _deadline(3600.0)
    near = _deadline(0.25)
    # The ceiling is what makes a configured timeout a bound rather than a
    # number: `exchange` and `_await_ready` are reachable with a timeout that
    # `BridgeConfig` never validated, because neither takes it from a config.
    assert far - started <= MAX_TIMEOUT_SECONDS + 1.0
    assert near - started <= 1.5


class _RecordsHowItWasJoined(threading.Thread):
    """A thread that never ends, and remembers the bound it was joined with."""

    def __init__(self) -> None:
        super().__init__(target=lambda: None, daemon=True)
        self.joined_with: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.joined_with.append(timeout)


def test_the_stop_path_joins_every_thread_with_a_bound_and_not_forever() -> None:
    # Asserted on the argument rather than on elapsed time: a `join()` with no
    # timeout against a thread that never ends does not make this test fail, it
    # makes the suite hang, and a hung suite reports nothing at all.
    bridge = JsonlBridge(a_config(stop_timeout=0.25))
    first, second = _RecordsHowItWasJoined(), _RecordsHowItWasJoined()
    bridge._threads = [first, second]
    bridge.stop()
    assert first.joined_with == [0.25]
    assert second.joined_with == [0.25]


def test_an_outcome_naming_a_request_that_is_not_a_handle_is_refused() -> None:
    line = f'{{"v":"1.0","type":"outcome","request_id":"{SECRET}","status":"ended"}}'
    with pytest.raises(MalformedMessage) as caught:
        read_outcome(decode(line.encode("utf-8")))
    assert SECRET not in str(caught.value)


# ---------------------------------------------------------------------------
# the two calls that promise never to fail, against the errors that are not
# BridgeError
# ---------------------------------------------------------------------------
#
# `BridgeProtocolError` is a `ValueError`, not a `BridgeError`, so `except
# BridgeError` does not catch it. Both calls below reach it: `send` restarts a
# crashed bridge, and the restart runs a handshake, and a handshake can meet a
# program that speaks another MAJOR. Before this was fixed, a spoken «стоп»
# raised `ProtocolMismatch` out of `interrupt`, and the transcript stream raised
# it through an `async for` instead of ending.


@pytest.mark.asyncio
async def test_an_interrupt_that_cannot_be_queued_is_still_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    client = TeamONBridgeClient(a_config(max_pending=1), listener=recorder)
    _running(client.bridge, monkeypatch)
    # Full, and nothing is draining it: no writer thread exists, because no
    # child was ever launched. The e2e test that fills a real outbox races the
    # real writer emptying a slot, so it does not establish this.
    client.bridge._outbox.put_nowait(b"{}\n")

    await client.interrupt("teamon-1")

    assert BridgeReason.DROPPED in recorder.reasons()


@pytest.mark.asyncio
async def test_an_interrupt_swallows_a_protocol_error_as_well_as_a_bridge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TeamONBridgeClient(a_config())

    def refuse(message: BridgeMessage) -> None:
        raise ProtocolMismatch("the bridge speaks protocol 9.0 and this build speaks 1.0")

    monkeypatch.setattr(client.bridge, "send", refuse)
    await client.interrupt("teamon-1")

    # And the other one that is not a BridgeError: a handle that is not a
    # handle is a bug on this side, and a stop word is not where it may surface.
    await client.interrupt("Not A Handle")


@pytest.mark.asyncio
async def test_the_transcript_stream_ends_on_a_protocol_error_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TeamONBridgeClient(a_config())

    def refuse(timeout: float) -> BridgeTranscript | None:
        raise ProtocolMismatch("the bridge speaks protocol 9.0 and this build speaks 1.0")

    monkeypatch.setattr(client.bridge, "next_transcript", refuse)
    stream = client._stream()
    # Ending is the contract. Raising through an `async for` is a companion that
    # stops listening *and* takes the caller's loop with it.
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


# ---------------------------------------------------------------------------
# a child that died is not a child that is slow
# ---------------------------------------------------------------------------
#
# The wait used to watch the pipe and nothing else, so the only way it could
# learn that the child was gone was EOF. That works wherever the operating
# system closes the child's handles before it does anything else, and it is not
# a promise any of them makes: a process handle is signalled only once every one
# of the child's threads has ended, and a handle inherited by something the
# child started outlives the child holding it. Where EOF is late the wait ran
# out and reported `BridgeTimeout` — "it did not answer in time", about a
# process that had already exited with a status somebody could have looked up.
#
# These are the two facts and the four orders they can arrive in, forced with an
# injected process rather than with a real one. A real child cannot be made to
# exit-without-EOF on demand — that is exactly the platform-dependent timing
# under test — so a double is the only way to assert on it from either platform,
# and the contract suite keeps the real-process claims it can actually make.


class _ExitedWith:
    """A child that has ended. Its `poll` answers; its pipe is at EOF."""

    pid = 4243

    def __init__(self, status: int) -> None:
        self._status = status
        self.stdin = None
        self.stdout = io.BytesIO(b"")

    def poll(self) -> int | None:
        return self._status


class _ExitedAndStillHoldingThePipe:
    """A child that has ended without its pipe reaching EOF.

    The shape a bridge takes when something it launched inherited its stdout, or
    when the platform marks the process finished before the handles close. The
    `read` never returns, which is what the reader thread would be doing, so the
    only evidence of the death is `poll`.
    """

    pid = 4245

    def __init__(self, status: int) -> None:
        self._status = status
        self.stdin = None
        self.stdout = None

    def poll(self) -> int | None:
        return self._status


class _StreamEndedButStillRunning:
    """A child at EOF whose `poll` has not caught up, which is the other order.

    Not hypothetical: `ExitProcess` closes the handles and the process object
    becomes signalled only after every thread in it has gone, so a child on its
    way out reads as running for the moment in between. A supervisor that asked
    `poll` alone would answer "still up" about a pipe nobody is holding, skip
    the restart, and queue the next message into the dark.
    """

    pid = 4244

    def __init__(self) -> None:
        self.stdin = None
        self.stdout = io.BytesIO(b"")

    def poll(self) -> int | None:
        return None


def _watching(bridge: JsonlBridge, process: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put *bridge* in the one state where a caller's wait is reached."""
    monkeypatch.setattr(bridge, "_state", BridgeState.RUNNING)
    monkeypatch.setattr(bridge, "_process", process)


def test_a_wait_that_meets_a_child_that_died_says_so_instead_of_timing_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows failure, made reproducible: exited, and no EOF to say so.

    The deadline is five seconds and the child is already gone, so a wait that
    watched only the pipe would sit here for all five of them and then call it a
    timeout. Both halves of the claim are asserted — that it came back early,
    and that what it came back with is a crash rather than a silence — because
    an implementation that returned early with a `BridgeTimeout` would satisfy
    either one alone.
    """
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    _watching(bridge, _ExitedAndStillHoldingThePipe(3), monkeypatch)

    started = time.monotonic()
    event = bridge._take_event(time.monotonic() + 5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the wait sat out its deadline against a child that had exited"
    with pytest.raises(BridgeUnavailable) as caught:
        bridge._handle_down(event)  # type: ignore[arg-type]
    # Not a timeout: a timeout says "wait longer", and there is nothing left to
    # wait for. And the status is in it, because 3 is a number somebody can go
    # and look up in the bridge program.
    assert "exited with status 3" in str(caught.value)
    assert recorder.reasons() == [BridgeReason.CRASHED]
    assert "exited with status 3" in recorder.details
    assert bridge.state is BridgeState.RUNNING, "a crash is restartable and must not be fatal"


def test_a_child_that_is_alive_and_silent_is_still_a_timeout_and_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the deadline exists for, which the fix above must not eat.

    A bridge that is running and has not answered yet is a real and different
    situation with a different remedy, and reporting it as a crash would restart
    a perfectly good child in the middle of a sentence.
    """
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    _watching(bridge, _AlwaysAlive(), monkeypatch)

    started = time.monotonic()
    with pytest.raises(BridgeTimeout):
        bridge._take_event(time.monotonic() + 0.3)

    assert time.monotonic() - started < 2.0
    assert recorder.reasons() == []


def test_a_wait_prefers_the_reply_that_did_arrive_over_the_death_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge that answered and then exited answered, and that answer counts.

    The outcome was written to the pipe before the child died, so it is on this
    side already. A wait that asked the process first would throw away a reply
    it is holding and report a crash instead — which would turn a synthesis that
    genuinely ended into a failure the user hears about.
    """
    bridge = JsonlBridge(a_config())
    _watching(bridge, _ExitedWith(3), monkeypatch)
    bridge._admit(b'{"v":"1.0","type":"outcome","request_id":"teamon-1","status":"ended"}', 0)

    event = bridge._take_event(time.monotonic() + 5.0)

    assert read_outcome(event).ended is True  # type: ignore[arg-type]


def test_a_child_whose_output_ended_is_not_alive_however_poll_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other order, and the one that leaves a bridge queueing into the dark.

    `poll` says the child is running and the reader has already seen its stream
    end. `alive` is what `_ensure_running` consults to decide whether to
    restart, so a supervisor that believed `poll` here would never restart and
    would report `RUNNING` about a bridge that had stopped.
    """
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    process = _StreamEndedButStillRunning()
    _watching(bridge, process, monkeypatch)

    # Both readings are taken rather than asserted in place: `poll` answers the
    # same thing on either side of the read loop, so the only thing that can
    # have changed the second one is what the reader saw.
    before = bridge.alive
    bridge._read_loop(process, bridge._generation)  # type: ignore[arg-type]
    after = bridge.alive

    assert (before, after) == (True, False)
    assert recorder.reasons() == [BridgeReason.CRASHED]
    # The status is genuinely not known here, so the sentence names what was
    # observed rather than inventing a code for it.
    assert "closed its output" in recorder.details


def test_the_reader_names_the_exit_status_when_the_system_already_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    process = _ExitedWith(3)
    _watching(bridge, process, monkeypatch)

    bridge._read_loop(process, bridge._generation)  # type: ignore[arg-type]

    assert recorder.reasons() == [BridgeReason.CRASHED]
    assert "exited with status 3" in recorder.details


def test_one_death_seen_by_both_halves_is_announced_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two watchers, one event, one report — and one `_Down`.

    The wait and the reader thread reach the same death by different routes and
    in either order. A second report would be two sentences in the log for one
    stop; the second `_Down` behind it would sit in the reply queue until the
    next exchange read it as *that* request failing.
    """
    recorder = Recorder()
    bridge = JsonlBridge(a_config(), listener=recorder)
    process = _ExitedWith(3)
    _watching(bridge, process, monkeypatch)

    taken = bridge._take_event(time.monotonic() + 5.0)
    bridge._read_loop(process, bridge._generation)  # type: ignore[arg-type]

    with pytest.raises(BridgeUnavailable):
        bridge._handle_down(taken)  # type: ignore[arg-type]
    assert recorder.reasons() == [BridgeReason.CRASHED]
    assert bridge._events.empty(), "the second watcher queued a death the first had reported"


def test_the_liveness_poll_is_a_bounded_slice_and_not_a_spin() -> None:
    """Bounded, and bounded at a value a caller's deadline still dominates.

    A slice this long costs ten wakeups a second, each of them spent blocked in
    `queue.get` rather than looping, and it bounds only how late "it died" can
    be. The literal is written out here rather than imported and compared
    against itself: what is being pinned is that the number is small enough to
    be a poll and large enough not to be a spin.
    """
    assert 0.01 <= _LIVENESS_POLL_SECONDS <= 0.25
    assert _LIVENESS_POLL_SECONDS < MAX_TIMEOUT_SECONDS


class _StillRunningAtEof:
    """A child at EOF that is genuinely still running, and can be stopped.

    The same shape as :class:`_StreamEndedButStillRunning`, with the stop path
    a real :class:`subprocess.Popen` offers: `poll` keeps answering ``None``
    until somebody ends it, which is what makes "was it ended" an observation
    rather than an assumption.
    """

    pid = 4246

    def __init__(self) -> None:
        self.stdin = None
        self.stdout = io.BytesIO(b"")
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        if not self.terminated:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=0.0 if timeout is None else timeout)
        return 0


def test_a_relaunch_does_not_leave_the_previous_child_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launching over a half-dead bridge must end it, not abandon it.

    `alive` is false as soon as *either* watcher saw the end, so a child whose
    stdout reached EOF while the process itself is still going now reads as not
    alive — and `start` consults exactly that before relaunching. `_restart`
    ends the old child first; `_launch` is reached directly from `start` and had
    no such step, so the relaunch would overwrite the only handle to a process
    that was still running and still holding the microphone.

    The launch itself fails, because the configured command is not a program.
    That is deliberate: the previous child must be ended before the new one is
    attempted, so a launch that never gets off the ground still cannot strand
    it.
    """
    bridge = JsonlBridge(a_config())
    process = _StillRunningAtEof()
    _watching(bridge, process, monkeypatch)
    bridge._read_loop(process, bridge._generation)  # type: ignore[arg-type]

    # The premise, asserted rather than assumed: not alive, and yet running.
    assert bridge.alive is False
    assert process.poll() is None, "the double stopped on its own; it proves nothing"

    with pytest.raises(BridgeUnavailable):
        bridge.start()

    assert process.terminated, "the relaunch left the previous child running"
    assert bridge._process is None
