"""The Core RPC schemas describe the bytes the code actually puts on the wire.

A schema nobody validates against is documentation that happens to be JSON, and
it drifts the first time the encoder changes. Both directions are checked here:
what :mod:`pz_agent_core.rpc.wire` emits must validate, and what the schema
permits must decode. Without the second, the schema could be tightened past what
a legitimate peer sends and nothing would notice until a real client failed.

The envelope is checked as *bytes*, not as the dataclass. The dataclass is what
this process holds; the bytes are what the other process reads, and they are
where a field-name change or a dropped key actually shows up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from pz_agent_core.rpc.wire import (
    ErrorCode,
    RpcError,
    RpcRequest,
    RpcResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from pz_agent_core.version import RPC_PROTOCOL_VERSION

pytestmark = pytest.mark.contract

SCHEMA_DIR: Final = Path(__file__).resolve().parents[2] / "schemas"


def _validator(name: str) -> Draft202012Validator:
    document = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(document)


@pytest.fixture(scope="module")
def request_schema() -> Draft202012Validator:
    return _validator("core_rpc_request.schema.json")


@pytest.fixture(scope="module")
def response_schema() -> Draft202012Validator:
    return _validator("core_rpc_response.schema.json")


def _sent(data: bytes) -> dict[str, Any]:
    """The document as the peer will parse it, not as we constructed it."""
    document: Any = json.loads(data.decode("utf-8"))
    assert isinstance(document, dict)
    return document


class TestOutbound:
    @pytest.mark.parametrize(
        "request_",
        [
            RpcRequest(id="a1b2", method="session.status"),
            RpcRequest(id="a1b2", method="session.arm", params={"mode": "ASSISTED"}),
            RpcRequest(id="a1b2", method="action.submit", params={"action": "EAT", "args": {}}),
            RpcRequest(id="a1b2", method="diagnostics.tail", params={"limit": 50}),
        ],
        ids=["status", "arm", "submit", "tail"],
    )
    def test_every_encoded_request_validates(
        self, request_schema: Draft202012Validator, request_: RpcRequest
    ) -> None:
        request_schema.validate(_sent(encode_request(request_)))

    def test_a_successful_answer_validates(self, response_schema: Draft202012Validator) -> None:
        response = RpcResponse(id="a1b2", ok=True, result={"mode": "OBSERVE", "armed": False})

        response_schema.validate(_sent(encode_response(response)))

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.MALFORMED,
            ErrorCode.TOO_LARGE,
            ErrorCode.PROTOCOL_MISMATCH,
            ErrorCode.UNKNOWN_METHOD,
            ErrorCode.CORE_REFUSED,
        ],
    )
    def test_every_error_the_server_can_send_validates(
        self, response_schema: Draft202012Validator, code: str
    ) -> None:
        """The enum in the schema has to hold every code the encoder can produce.

        A code missing from it would validate in tests that only exercise the
        happy path, and fail the first time that failure actually happened.
        """
        response = RpcResponse(id="a1b2", ok=False, error_code=code, error_message="why")

        response_schema.validate(_sent(encode_response(response)))

    def test_the_oversized_answer_the_encoder_substitutes_also_validates(
        self, response_schema: Draft202012Validator
    ) -> None:
        """That substitution is built by hand rather than by `encode_response`."""
        response = RpcResponse(id="a1b2", ok=True, result={"pad": "x" * 8_000_000})

        document = _sent(encode_response(response))

        response_schema.validate(document)
        assert document["error"]["code"] == ErrorCode.TOO_LARGE

    def test_the_version_on_the_wire_is_the_one_the_schema_pins(
        self, request_schema: Draft202012Validator
    ) -> None:
        document = _sent(encode_request(RpcRequest(id="a1b2", method="session.status")))

        assert document["protocol"] == RPC_PROTOCOL_VERSION


class TestInbound:
    def test_a_document_the_schema_permits_decodes(
        self, request_schema: Draft202012Validator
    ) -> None:
        """Otherwise the schema could be widened past what the decoder accepts."""
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "z" * 64,
            "method": "capabilities.report",
            "params": {},
        }
        request_schema.validate(document)

        decoded = decode_request(json.dumps(document).encode("utf-8"))

        assert decoded.method == "capabilities.report"

    def test_a_response_the_schema_permits_decodes(
        self, response_schema: Draft202012Validator
    ) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": RPC_PROTOCOL_VERSION,
            "id": "a1b2",
            "ok": False,
            "error": {"code": ErrorCode.CORE_REFUSED, "message": "no backup confirmed"},
        }
        response_schema.validate(document)

        decoded = decode_response(json.dumps(document).encode("utf-8"))

        assert decoded.error_code == ErrorCode.CORE_REFUSED


class TestTheSchemaRefusesWhatTheDecoderRefuses:
    """Where the two disagree, one of them is wrong. These pin them together."""

    @pytest.mark.parametrize(
        "document",
        [
            {"format": "pz-agent-core-rpc/1", "protocol": "1.0", "method": "a.b", "params": {}},
            {"format": "pz-agent-core-rpc/1", "protocol": "1.0", "id": "x", "params": {}},
            {
                "format": "somebody-else/1",
                "protocol": "1.0",
                "id": "x",
                "method": "a.b",
                "params": {},
            },
            {
                "format": "pz-agent-core-rpc/1",
                "protocol": "1.0",
                "id": "x",
                "method": "a.b",
                "params": [],
            },
            {
                "format": "pz-agent-core-rpc/1",
                "protocol": "1.0",
                "id": "",
                "method": "a.b",
                "params": {},
            },
        ],
        ids=["no-id", "no-method", "foreign-format", "params-not-object", "empty-id"],
    )
    def test_a_request_neither_accepts(
        self, request_schema: Draft202012Validator, document: dict[str, Any]
    ) -> None:
        assert not request_schema.is_valid(document), "the schema accepted it"
        with pytest.raises(RpcError):
            decode_request(json.dumps(document).encode("utf-8"))

    def test_a_response_with_no_ok_flag_is_refused_by_both(
        self, response_schema: Draft202012Validator
    ) -> None:
        document = {
            "format": "pz-agent-core-rpc/1",
            "protocol": "1.0",
            "id": "x",
            "result": {},
        }

        assert not response_schema.is_valid(document)
        with pytest.raises(RpcError):
            decode_response(json.dumps(document).encode("utf-8"))

    def test_a_successful_response_must_carry_a_result(
        self, response_schema: Draft202012Validator
    ) -> None:
        """`ok` with nothing to report is `{}`, not an absent field."""
        document = {"format": "pz-agent-core-rpc/1", "protocol": "1.0", "id": "x", "ok": True}

        assert not response_schema.is_valid(document)

    def test_a_failed_response_must_carry_an_error(
        self, response_schema: Draft202012Validator
    ) -> None:
        document = {"format": "pz-agent-core-rpc/1", "protocol": "1.0", "id": "x", "ok": False}

        assert not response_schema.is_valid(document)
        with pytest.raises(RpcError):
            decode_response(json.dumps(document).encode("utf-8"))
