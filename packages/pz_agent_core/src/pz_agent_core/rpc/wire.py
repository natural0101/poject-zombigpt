"""The envelope on the Local Core RPC link, and every reason to refuse one.

Two processes on one machine: the sidecar, which owns the session, the
observation store and the action engine, and the MCP executable, which an MCP
client launches and which owns none of that. Everything the boundary reports it
has to ask for, and this module is the format of the asking.

**JSON, never pickle.** :mod:`multiprocessing.connection` offers ``send`` and
``recv``, which pickle. A pickle stream is code execution by construction: a
process that can write to the pipe can run anything in the process reading it,
and the token below authenticates the *connection*, not each message. So only
``send_bytes``/``recv_bytes`` are used, the payload is UTF-8 JSON, and
:func:`decode_request` refuses anything that is not an object with the fields it
expects. There is no code path in this package that unpickles.

**Bounded on both sides.** A request is capped because a client can send one,
and a response is capped because an observation of a nested inventory is the
largest thing the core hands back and an unbounded one would let a single query
exhaust the sidecar. Both caps are enforced before the bytes are parsed — a
length check after ``json.loads`` has already paid the cost it was meant to
avoid.

**The refusal never quotes the payload.** An error message that echoed what it
rejected would put game text, a path, or a token into a log line that is not
redacted, and this envelope is the one place a malformed message is described.
So errors name the field and the reason, never the value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from pz_agent_core.jsonbytes import deepest_nesting
from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import RPC_PROTOCOL_VERSION, protocol_major

__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "ErrorCode",
    "ProtocolMismatch",
    "RpcError",
    "RpcRequest",
    "RpcResponse",
    "TooLarge",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]

#: Largest request the server will read. A request is a method name and a small
#: parameter object; the biggest real one submits an action, which is a few
#: hundred bytes. 64 KiB is two orders of magnitude of headroom and still small
#: enough that a client cannot make the server allocate meaningfully.
MAX_REQUEST_BYTES: Final = 64 * 1024

#: Largest response the client will read. Larger than the request cap because
#: the core answers with observations: a tier-2 snapshot of a nested inventory
#: is the biggest document this system has. Still a cap, because "however big
#: the core made it" is not a bound.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

#: Longest method name accepted. Names are dotted identifiers from a closed set;
#: this is a cheap guard applied before any lookup.
MAX_METHOD_CHARS: Final = 64

#: Longest request id accepted. Ids are opaque to the server and echoed back.
MAX_ID_CHARS: Final = 64

_ENVELOPE_FORMAT: Final = "pz-agent-core-rpc/1"

#: Deepest nesting of arrays and objects a frame may carry. The envelope is
#: shallow — five top-level fields, whose ``params`` is one object of scalars
#: and at most a small structure — so a bound this generous never refuses a
#: real message. It exists because ``json.loads`` recurses once per nesting
#: level, and a frame of ``[`` repeated a thousand times, well under the byte
#: cap, overflows the interpreter's stack with a ``RecursionError`` the byte
#: cap cannot see. The depth is measured on the raw bytes *before* parsing, so
#: the parser is never handed a document that could make it recurse past here —
#: catching ``RecursionError`` after the fact is not safe, because the stack is
#: already near its limit when it fires.
MAX_NESTING_DEPTH: Final = 64


class ErrorCode:
    """The closed set of failures the envelope itself can report.

    A method's own refusal travels as ``CORE_REFUSED`` with the core's message;
    these are the ones that mean the message never reached a method.
    """

    MALFORMED: Final = "MALFORMED"
    TOO_LARGE: Final = "TOO_LARGE"
    PROTOCOL_MISMATCH: Final = "PROTOCOL_MISMATCH"
    UNKNOWN_METHOD: Final = "UNKNOWN_METHOD"
    CORE_REFUSED: Final = "CORE_REFUSED"
    TIMEOUT: Final = "TIMEOUT"
    UNAVAILABLE: Final = "UNAVAILABLE"


class RpcError(Exception):
    """A message could not be understood, or a call could not be completed."""

    code: str = ErrorCode.MALFORMED


class TooLarge(RpcError):
    """A payload exceeded its cap. The payload is never included."""

    code = ErrorCode.TOO_LARGE


class ProtocolMismatch(RpcError):
    """The peer speaks an incompatible major version of this envelope.

    Refused rather than attempted. The realistic cause is an executable left
    behind by an earlier install talking to a newer sidecar, and a client that
    guessed at the difference would report the core's state incorrectly rather
    than not at all.
    """

    code = ErrorCode.PROTOCOL_MISMATCH


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """One call: an id the client chose, a method, and its parameters."""

    id: str
    method: str
    params: JsonDict = field(default_factory=dict)
    protocol: str = RPC_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """One answer. Exactly one of *result* and *error* is meaningful.

    ``ok`` is stored rather than derived from ``error is None`` so that a
    response saying "succeeded with nothing to report" is distinguishable from
    one whose error was lost in transit. A reader that inferred success from an
    absent error would turn a dropped field into a success, which is the failure
    mode this project treats as the worst one.
    """

    id: str
    ok: bool
    result: JsonDict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ""
    protocol: str = RPC_PROTOCOL_VERSION


def _dumps(document: JsonDict) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_request(request: RpcRequest) -> bytes:
    """*request* as the bytes to put on the wire.

    Raises:
        TooLarge: when the encoded form exceeds :data:`MAX_REQUEST_BYTES`. Caught
            here rather than at the socket so the caller learns which request was
            too big while it still has it.
    """
    data = _dumps(
        {
            "format": _ENVELOPE_FORMAT,
            "protocol": request.protocol,
            "id": request.id,
            "method": request.method,
            "params": dict(request.params),
        }
    )
    if len(data) > MAX_REQUEST_BYTES:
        raise TooLarge(f"request {request.method} is {len(data)} bytes, cap is {MAX_REQUEST_BYTES}")
    return data


def encode_response(response: RpcResponse) -> bytes:
    """*response* as the bytes to put on the wire.

    A response that overflows the cap is replaced by an error response rather
    than raised: the client is waiting, and a server that answered nothing would
    leave it to the deadline. The error names the size, never the content.
    """
    document: JsonDict = {
        "format": _ENVELOPE_FORMAT,
        "protocol": response.protocol,
        "id": response.id,
        "ok": response.ok,
    }
    if response.ok:
        document["result"] = dict(response.result)
    else:
        document["error"] = {
            "code": response.error_code or ErrorCode.MALFORMED,
            "message": response.error_message,
        }
    data = _dumps(document)
    if len(data) > MAX_RESPONSE_BYTES:
        return _dumps(
            {
                "format": _ENVELOPE_FORMAT,
                "protocol": response.protocol,
                "id": response.id,
                "ok": False,
                "error": {
                    "code": ErrorCode.TOO_LARGE,
                    "message": (
                        f"the answer is {len(data)} bytes, cap is {MAX_RESPONSE_BYTES}; "
                        "narrow the request"
                    ),
                },
            }
        )
    return data


def _loaded(data: bytes, cap: int, what: str) -> dict[str, Any]:
    if len(data) > cap:
        raise TooLarge(f"{what} is {len(data)} bytes, cap is {cap}")
    # Depth is measured on the raw bytes before parsing — see
    # :mod:`pz_agent_core.jsonbytes` for why catching the ``RecursionError``
    # afterwards is not a safe recovery.
    if deepest_nesting(data) > MAX_NESTING_DEPTH:
        raise RpcError(f"{what} nests deeper than {MAX_NESTING_DEPTH}")
    try:
        document: Any = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RpcError(f"{what} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        # `exc` carries a position, never the text, which is why it is safe to
        # name here at all.
        raise RpcError(f"{what} is not JSON (at char {exc.pos})") from None
    except ValueError as exc:
        # A ``ValueError`` that is not a ``JSONDecodeError``: the parser's own
        # refusal of a number too extreme to build, which on CPython is the
        # integer-string-conversion ceiling firing on a literal of thousands of
        # digits — inside the byte cap, outside what any real parameter carries.
        # Named as malformed without echoing the digits.
        raise RpcError(f"{what} carries a number the parser refuses") from exc
    if not isinstance(document, dict):
        raise RpcError(f"{what} must be a JSON object")
    # The first thing that separates our bytes from anything else that happens
    # to be JSON arriving on this pipe. Checked here rather than per-message so
    # neither direction can be the one that forgot; the schemas require it, and
    # a decoder that did not was the schemas describing a stricter format than
    # the code enforced.
    marker = document.get("format")
    if marker != _ENVELOPE_FORMAT:
        raise RpcError(f"{what} is not a {_ENVELOPE_FORMAT} envelope")
    return document


def _checked_protocol(document: dict[str, Any], what: str) -> str:
    protocol = document.get("protocol")
    if not isinstance(protocol, str) or not protocol:
        raise RpcError(f"{what} has no protocol version")
    try:
        theirs = protocol_major(protocol)
    except ValueError as exc:
        raise RpcError(f"{what} has an unreadable protocol version") from exc
    if theirs != protocol_major(RPC_PROTOCOL_VERSION):
        raise ProtocolMismatch(
            f"{what} speaks protocol {protocol}, this build speaks {RPC_PROTOCOL_VERSION}"
        )
    return protocol


def _text(document: dict[str, Any], key: str, cap: int, what: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(f"{what} has no {key}")
    if len(value) > cap:
        raise RpcError(f"{what} has a {key} of {len(value)} characters, cap is {cap}")
    return value


def decode_request(data: bytes) -> RpcRequest:
    """Parse a request, or refuse it.

    Raises:
        TooLarge: over :data:`MAX_REQUEST_BYTES`.
        ProtocolMismatch: incompatible major version.
        RpcError: anything else — not JSON, not an object, missing a field.
    """
    document = _loaded(data, MAX_REQUEST_BYTES, "request")
    protocol = _checked_protocol(document, "request")
    params = document.get("params", {})
    if not isinstance(params, dict):
        raise RpcError("request params must be a JSON object")
    return RpcRequest(
        id=_text(document, "id", MAX_ID_CHARS, "request"),
        method=_text(document, "method", MAX_METHOD_CHARS, "request"),
        params=params,
        protocol=protocol,
    )


def decode_response(data: bytes) -> RpcResponse:
    """Parse a response, or refuse it.

    ``ok`` must be present and boolean. A response missing it is refused rather
    than defaulted: defaulting to True invents a success, and defaulting to
    False invents a failure with no reason, and neither is something a caller
    should be handed as if the server had said it.
    """
    document = _loaded(data, MAX_RESPONSE_BYTES, "response")
    protocol = _checked_protocol(document, "response")
    identifier = _text(document, "id", MAX_ID_CHARS, "response")
    ok = document.get("ok")
    if not isinstance(ok, bool):
        raise RpcError("response has no ok flag")
    if ok:
        result = document.get("result", {})
        if not isinstance(result, dict):
            raise RpcError("response result must be a JSON object")
        return RpcResponse(id=identifier, ok=True, result=result, protocol=protocol)
    error = document.get("error")
    if not isinstance(error, dict):
        raise RpcError("failed response has no error object")
    code = error.get("code")
    message = error.get("message", "")
    if not isinstance(code, str) or not code:
        raise RpcError("failed response has no error code")
    if not isinstance(message, str):
        raise RpcError("failed response has an unreadable error message")
    return RpcResponse(
        id=identifier,
        ok=False,
        error_code=code,
        error_message=message,
        protocol=protocol,
    )
