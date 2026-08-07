"""The RPC envelope: what it accepts, what it refuses, and what it never says.

Three claims are load-bearing here and each has its own section.

**Nothing in this package unpickles.** The transport offers a pickling `send`
and a byte-oriented `send_bytes`, and the difference between them is arbitrary
code execution in the process that reads. The test that matters is not that JSON
works — it is that a pickle stream is refused, so a wiring change that reached
for the convenient call fails here rather than in a user's process.

**Both directions are bounded.** A cap that is checked after parsing has already
paid the cost it was there to avoid.

**A refusal never quotes what it rejected.** The envelope is where a malformed
message is described, and that description is written before any redactor sees
it — into a log line, a traceback, and eventually a bug report.
"""

from __future__ import annotations

import json
import pickle
from typing import Final

import pytest

from pz_agent_core.rpc.wire import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ErrorCode,
    ProtocolMismatch,
    RpcError,
    RpcRequest,
    RpcResponse,
    TooLarge,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from pz_agent_core.version import RPC_PROTOCOL_VERSION

SECRET: Final = "sk-live-9f8e7d6c5b4a3210"


class TestRoundTrip:
    def test_a_request_survives_the_wire_unchanged(self) -> None:
        request = RpcRequest(id="abc123", method="session.status", params={"verbose": True})

        assert decode_request(encode_request(request)) == request

    def test_a_successful_response_survives_the_wire_unchanged(self) -> None:
        response = RpcResponse(id="abc123", ok=True, result={"mode": "OBSERVE", "armed": False})

        assert decode_response(encode_response(response)) == response

    def test_a_failed_response_survives_the_wire_unchanged(self) -> None:
        response = RpcResponse(
            id="abc123",
            ok=False,
            error_code=ErrorCode.CORE_REFUSED,
            error_message="no backup confirmed",
        )

        assert decode_response(encode_response(response)) == response

    def test_cyrillic_survives_as_text_rather_than_escapes(self) -> None:
        """The whole project is built for a Russian-language install."""
        response = RpcResponse(id="1", ok=True, result={"label": "Пользователь"})

        decoded = decode_response(encode_response(response))

        assert decoded.result["label"] == "Пользователь"


class TestPickleIsNotAcceptedAnywhere:
    """A pickle stream reaching a parser is remote code execution.

    `multiprocessing.connection` hands you `send`/`recv`, which pickle, right
    next to `send_bytes`/`recv_bytes`, which do not. These tests fail if a
    decoder ever grows a pickle path — including via a "be liberal in what you
    accept" fallback, which is the plausible way it would be reintroduced.
    """

    @pytest.mark.parametrize("payload", [{"method": "x"}, ["a"], "text", 42])
    def test_a_pickle_stream_is_refused_rather_than_loaded(self, payload: object) -> None:
        data = pickle.dumps(payload)

        with pytest.raises(RpcError):
            decode_request(data)
        with pytest.raises(RpcError):
            decode_response(data)

    def test_a_pickle_that_would_execute_on_load_is_refused(self) -> None:
        """The actual attack, not a benign object that happens to be pickled."""

        class Detonator:
            def __reduce__(self) -> tuple[object, tuple[str, ...]]:
                return (exec, ("raise AssertionError('the decoder unpickled')",))

        with pytest.raises(RpcError):
            decode_request(pickle.dumps(Detonator()))


class TestCaps:
    def test_an_oversized_request_is_refused_before_it_is_sent(self) -> None:
        request = RpcRequest(
            id="1", method="action.submit", params={"pad": "x" * MAX_REQUEST_BYTES}
        )

        with pytest.raises(TooLarge):
            encode_request(request)

    def test_an_oversized_request_is_refused_on_arrival_too(self) -> None:
        """A peer that does not use `encode_request` is still bounded."""
        data = json.dumps(
            {
                "format": "pz-agent-core-rpc/1",
                "protocol": RPC_PROTOCOL_VERSION,
                "id": "1",
                "method": "x",
                "params": {"pad": "x" * MAX_REQUEST_BYTES},
            }
        ).encode("utf-8")

        with pytest.raises(TooLarge):
            decode_request(data)

    def test_an_oversized_answer_becomes_an_error_rather_than_silence(self) -> None:
        """The client is waiting; a server that sent nothing would hit the deadline."""
        response = RpcResponse(id="1", ok=True, result={"pad": "x" * MAX_RESPONSE_BYTES})

        decoded = decode_response(encode_response(response))

        assert decoded.ok is False
        assert decoded.error_code == ErrorCode.TOO_LARGE
        assert decoded.id == "1", "the client cannot match an answer with no id"

    def test_the_cap_is_applied_to_the_bytes_not_the_parsed_document(self) -> None:
        """Otherwise the allocation the cap exists to prevent has already happened."""
        data = b'{"protocol": "1.0", "id": "1", "method": "x", "params": {}}' + b" " * (
            MAX_REQUEST_BYTES + 1
        )

        with pytest.raises(TooLarge):
            decode_request(data)

    @pytest.mark.parametrize("field", ["id", "method"])
    def test_an_unbounded_string_field_is_refused(self, field: str) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "1",
            "method": "x",
            "params": {},
        }
        document[field] = "y" * 5000

        with pytest.raises(RpcError, match="cap is"):
            decode_request(json.dumps(document).encode("utf-8"))


class TestProtocolVersion:
    def test_a_different_major_is_refused(self) -> None:
        """A stale executable from an earlier install, talking to a new sidecar."""
        data = encode_request(RpcRequest(id="1", method="x")).replace(
            b'"protocol":"1.0"', b'"protocol":"2.0"'
        )

        with pytest.raises(ProtocolMismatch):
            decode_request(data)

    def test_a_different_minor_is_accepted(self) -> None:
        """Minor versions add methods; refusing them would make every addition a break."""
        data = encode_request(RpcRequest(id="1", method="x")).replace(
            b'"protocol":"1.0"', b'"protocol":"1.7"'
        )

        assert decode_request(data).protocol == "1.7"

    @pytest.mark.parametrize("spelling", [b'"x.y"', b'""', b"null", b"1.0", b"[]"])
    def test_an_unreadable_version_is_a_refusal_not_a_guess(self, spelling: bytes) -> None:
        data = encode_request(RpcRequest(id="1", method="x")).replace(b'"1.0"', spelling, 1)

        with pytest.raises(RpcError):
            decode_request(data)

    def test_a_bare_major_is_read_as_that_major(self) -> None:
        """`"1"` is not malformed, and this envelope does not get to disagree.

        `protocol_major` is shared with the mod↔sidecar protocol, where a bare
        major is accepted. Being stricter here would mean two versions of "what
        a version looks like" in one codebase, and the stricter one would refuse
        a peer the rest of the system talks to.
        """
        data = encode_request(RpcRequest(id="1", method="x")).replace(b'"1.0"', b'"1"', 1)

        assert decode_request(data).protocol == "1"


class TestMalformed:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"not json",
            b"[]",
            b'"a string"',
            b"null",
            b"\xff\xfe not utf-8",
        ],
    )
    def test_a_message_that_is_not_an_object_is_refused(self, data: bytes) -> None:
        with pytest.raises(RpcError):
            decode_request(data)

    @pytest.mark.parametrize("missing", ["id", "method", "protocol"])
    def test_a_request_missing_a_required_field_is_refused(self, missing: str) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "1",
            "method": "x",
            "params": {},
        }
        del document[missing]

        with pytest.raises(RpcError):
            decode_request(json.dumps(document).encode("utf-8"))

    def test_params_must_be_an_object(self) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "1",
            "method": "x",
            "params": ["not", "an", "object"],
        }

        with pytest.raises(RpcError, match="params"):
            decode_request(json.dumps(document).encode("utf-8"))

    def test_a_response_with_no_ok_flag_is_refused_rather_than_defaulted(self) -> None:
        """Defaulting invents either a success or a failure with no reason.

        Both are worse than saying the answer was unreadable, because both look
        like something the server said.
        """
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "1",
            "result": {},
        }

        with pytest.raises(RpcError, match="ok"):
            decode_response(json.dumps(document).encode("utf-8"))

    def test_a_failed_response_with_no_error_code_is_refused(self) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "1",
            "ok": False,
            "error": {"message": "something"},
        }

        with pytest.raises(RpcError, match="error code"):
            decode_response(json.dumps(document).encode("utf-8"))


class TestARefusalNeverQuotesThePayload:
    """The error text is written before any redactor runs, into a traceback."""

    def test_a_malformed_request_error_does_not_echo_it(self) -> None:
        data = f'{{"method": "x", "token": "{SECRET}"'.encode()

        with pytest.raises(RpcError) as caught:
            decode_request(data)

        assert SECRET not in str(caught.value)
        assert "token" not in str(caught.value)

    def test_an_oversized_request_error_names_the_size_and_not_the_content(self) -> None:
        request = RpcRequest(id="1", method="x", params={"pad": SECRET * 4000})

        with pytest.raises(TooLarge) as caught:
            encode_request(request)

        assert SECRET not in str(caught.value)
        assert "cap is" in str(caught.value)

    def test_an_oversized_answer_error_names_the_size_and_not_the_content(self) -> None:
        response = RpcResponse(id="1", ok=True, result={"pad": SECRET * 300_000})

        decoded = decode_response(encode_response(response))

        assert SECRET not in decoded.error_message

    def test_a_field_error_names_the_field_and_not_its_value(self) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": SECRET * 10,
            "method": "x",
            "params": {},
        }

        with pytest.raises(RpcError) as caught:
            decode_request(json.dumps(document).encode("utf-8"))

        assert SECRET not in str(caught.value)
