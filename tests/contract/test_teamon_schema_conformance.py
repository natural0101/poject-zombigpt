"""The bridge schema describes the lines the code actually puts on the pipe.

Same contract as ``test_core_rpc_schema_conformance.py``, for the voice bridge:
what :func:`~pz_agent_voice.teamon.encode` emits must validate against
``schemas/teamon_bridge.schema.json``, and what the schema permits must decode.
The schema is the document a bridge implementer builds against — ``docs/VOICE.md``
sends them there — so a schema looser than the decoder would ship a bridge whose
messages this build refuses, and a schema tighter than the encoder would reject
this build's own lines.

The closed sets are compared *as sets* against the enums in ``teamon.py``: the
message types, the goal tokens and the outcome statuses each exist in exactly
two places (the code and the schema), and this file is what keeps them from
drifting apart. The error codes are deliberately not enforced by the schema —
a reader must survive a code from a newer bridge — so they are checked here as
documentation (the schema's description names the openness) rather than as an
enum.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from pz_agent_voice.messages import MAX_TEXT_CHARS, VoiceGoal
from pz_agent_voice.teamon import (
    BRIDGE_PROTOCOL_VERSION,
    BridgeDirection,
    BridgeMessage,
    BridgeMessageType,
    OutcomeStatus,
    decode,
    encode,
    goal_message,
    hello_message,
    interrupt_message,
    read_outcome,
    read_ready,
    read_transcript,
    speak_message,
)

pytestmark = pytest.mark.contract

SCHEMA_PATH: Final = Path(__file__).resolve().parents[2] / "schemas" / "teamon_bridge.schema.json"


@pytest.fixture(scope="module")
def schema() -> Draft202012Validator:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(document)


def _document(schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    loaded: Any = json.loads(schema_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _sent(data: bytes) -> dict[str, Any]:
    """The line as the bridge will parse it: one object, one trailing newline."""
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    document: Any = json.loads(data[:-1].decode("utf-8"))
    assert isinstance(document, dict)
    return document


class TestOutbound:
    """Every line this build can emit validates against the schema."""

    @pytest.mark.parametrize(
        "message",
        [
            hello_message(),
            speak_message(utterance_id="utt-1", text="Иду есть.", priority=0, interruptible=True),
            speak_message(
                utterance_id="utt-2", text="x" * MAX_TEXT_CHARS, priority=7, interruptible=False
            ),
            interrupt_message("utt-1"),
            *[goal_message(request_id=f"req-{member.value}", goal=member) for member in VoiceGoal],
        ],
        ids=lambda m: str(m.type) if isinstance(m, BridgeMessage) else repr(m),
    )
    def test_every_encoded_message_validates(
        self, schema: Draft202012Validator, message: BridgeMessage
    ) -> None:
        schema.validate(_sent(encode(message)))


class TestInbound:
    """Every line the schema permits from the bridge is one the readers accept."""

    def test_a_schema_valid_ready_decodes(self, schema: Draft202012Validator) -> None:
        line = {"v": BRIDGE_PROTOCOL_VERSION, "type": "ready"}
        schema.validate(line)
        message = decode(json.dumps(line).encode("utf-8"))
        assert read_ready(message) == (1, 0)

    def test_a_schema_valid_transcript_decodes(self, schema: Draft202012Validator) -> None:
        line = {
            "v": BRIDGE_PROTOCOL_VERSION,
            "type": "transcript",
            "text": "иди есть",
            "at_ms": 1200,
            "final": True,
            "confidence": 0.9,
        }
        schema.validate(line)
        transcript = read_transcript(decode(json.dumps(line).encode("utf-8")))
        assert transcript.text == "иди есть"
        assert transcript.final is True

    @pytest.mark.parametrize("status", [member.value for member in OutcomeStatus])
    def test_every_schema_valid_outcome_status_decodes(
        self, schema: Draft202012Validator, status: str
    ) -> None:
        line = {
            "v": BRIDGE_PROTOCOL_VERSION,
            "type": "outcome",
            "request_id": "req-1",
            "status": status,
        }
        schema.validate(line)
        outcome = read_outcome(decode(json.dumps(line).encode("utf-8")))
        assert outcome.status.value == status


class TestTheClosedSetsAgree:
    """The schema and the code name the same sets, checked set-for-set."""

    def test_the_message_types_are_the_schemas_type_enum(self) -> None:
        document = _document()
        assert set(document["properties"]["type"]["enum"]) == {
            member.value for member in BridgeMessageType
        }

    def test_every_message_type_has_exactly_one_branch(self) -> None:
        branches = [branch["properties"]["type"]["const"] for branch in _document()["oneOf"]]
        assert sorted(branches) == sorted(member.value for member in BridgeMessageType)

    def test_the_goal_tokens_are_the_voice_goal_enum(self) -> None:
        goal_branch = next(
            branch
            for branch in _document()["oneOf"]
            if branch["properties"]["type"]["const"] == "goal"
        )
        assert set(goal_branch["properties"]["goal"]["enum"]) == {
            member.value for member in VoiceGoal
        }

    def test_the_outcome_statuses_are_the_outcome_enum(self) -> None:
        outcome_branch = next(
            branch
            for branch in _document()["oneOf"]
            if branch["properties"]["type"]["const"] == "outcome"
        )
        assert set(outcome_branch["properties"]["status"]["enum"]) == {
            member.value for member in OutcomeStatus
        }

    def test_the_utterance_cap_is_the_codes_cap(self) -> None:
        speak_branch = next(
            branch
            for branch in _document()["oneOf"]
            if branch["properties"]["type"]["const"] == "speak"
        )
        assert speak_branch["properties"]["text"]["maxLength"] == MAX_TEXT_CHARS


class TestTheSchemaRefusesWhatTheCodeRefuses:
    """Negative space: a line the decoder would refuse must not validate.

    Not exhaustive — the decoder also refuses on size and direction, which a
    JSON schema cannot express — but each case here is one a bridge implementer
    could plausibly ship, and the schema is their first line of defence.
    """

    @pytest.mark.parametrize(
        "line",
        [
            {"type": "ready"},
            {"v": BRIDGE_PROTOCOL_VERSION, "type": "no_such_type"},
            {"v": BRIDGE_PROTOCOL_VERSION, "type": "outcome", "request_id": "r", "status": "done"},
            {"v": BRIDGE_PROTOCOL_VERSION, "type": "goal", "request_id": "r", "goal": "иди есть"},
            {"v": "one.zero", "type": "ready"},
            {"v": BRIDGE_PROTOCOL_VERSION, "type": "transcript", "text": "x"},
        ],
        ids=[
            "no-version",
            "unknown-type",
            "invented-status",
            "transcript-as-goal",
            "unversion",
            "transcript-without-timestamp",
        ],
    )
    def test_it_does_not_validate(self, schema: Draft202012Validator, line: dict[str, Any]) -> None:
        assert not schema.is_valid(line)

    def test_direction_is_beyond_a_schema_and_the_decoder_owns_it(self) -> None:
        """The one refusal the schema cannot make, named so nobody expects it to.

        A ``speak`` travelling *from* the bridge validates against the schema —
        it is a well-formed speak — and the decoder still refuses it, because
        direction is a property of the session, not of the line.
        """
        line = encode(
            speak_message(utterance_id="utt-1", text="эхо", priority=0, interruptible=True)
        )[:-1]
        with pytest.raises(Exception, match="travels"):
            decode(line, expect=BridgeDirection.FROM_BRIDGE)
