"""Hostile callers against a real RPC server, over the family this machine binds.

:mod:`tests.unit.test_rpc_transport` proves the honest paths and the bounded
waits: a wrong key is refused, a mute peer is given up on, an over-cap frame is
dropped at the header, a link cut mid-exchange is reported. What it does not
exercise is a peer that *authenticates honestly* and then abuses the protocol
itself — frames that pass the challenge and the length cap and are hostile only
in their content. That caller is realistic: the token file is mode 0600, but
any process of the same user can read it, so "holds the key" and "is our
client" are not the same claim. Everything past the handshake must therefore
be answered as data — a typed error envelope, never a crash, never an echo of
the hostile value, never a served thread held hostage — and the server must
still be serving the next, honest caller afterwards. Each test here ends on
that observation rather than assuming it.

The raw frames are built by hand, format marker spelled out as a string
literal, because that is what an adversary does: nothing in these payloads went
through :func:`encode_request`'s own validation unless the test says so.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import count
from multiprocessing.connection import Client, Connection
from pathlib import Path
from typing import Protocol

import pytest

from pz_agent_core.rpc.descriptor import FAMILY_PIPE, load_descriptor, runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token, read_token
from pz_agent_core.rpc.transport import RpcClient, RpcServer, RpcUnavailable, new_address
from pz_agent_core.rpc.wire import (
    MAX_ID_CHARS,
    MAX_METHOD_CHARS,
    MAX_REQUEST_BYTES,
    ErrorCode,
    RpcRequest,
    RpcResponse,
    decode_response,
    encode_request,
)
from pz_agent_core.version import RPC_PROTOCOL_VERSION

#: Long enough that a loaded CI runner does not fail the happy path, short
#: enough that a genuine hang ends the test rather than the suite's patience.
GRACE: float = 10.0


class Start(Protocol):
    """What the `serving` fixture hands back: start a server with this handler."""

    def __call__(self, handler: Callable[[RpcRequest], RpcResponse]) -> Harness: ...


@dataclass
class Harness:
    server: RpcServer
    thread: threading.Thread
    key: bytes
    runtime: Path

    def client(self, *, deadline: float = GRACE, key: bytes | None = None) -> RpcClient:
        return RpcClient(self.server.descriptor(), authkey=key or self.key, deadline=deadline)

    def raw(self) -> Connection:
        """An honestly authenticated connection the test then writes raw bytes on."""
        family = "AF_PIPE" if self.server.family == FAMILY_PIPE else "AF_UNIX"
        return Client(self.server.address, family=family, authkey=self.key)


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[Start]:
    started: list[Harness] = []
    numbered = count()

    def start(handler: Callable[[RpcRequest], RpcResponse]) -> Harness:
        # A numbered runtime per server, so a test that starts two gets two
        # addresses and two token files rather than the second clobbering the
        # first's key — the wrong-live-server test depends on that.
        runtime = tmp_path / f"runtime-{next(numbered)}"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        harness = Harness(server=server, thread=thread, key=key, runtime=runtime)
        started.append(harness)
        return harness

    yield start

    for harness in started:
        harness.server.close()
        harness.thread.join(timeout=GRACE)


def _router(request: RpcRequest) -> RpcResponse:
    """The shape of the sidecar's dispatch: a closed catalogue of one method.

    The error message deliberately does not repeat the method name — the wire
    module's rule is that a refusal never quotes the payload, and a router that
    echoed an attacker-chosen method into a log line would break it one layer
    up from where the tests below check it.
    """
    if request.method != "session.status":
        return RpcResponse(
            id=request.id,
            ok=False,
            error_code=ErrorCode.UNKNOWN_METHOD,
            error_message="no such method in this build's catalogue",
        )
    return RpcResponse(id=request.id, ok=True, result={"served": True})


def _envelope(
    *,
    id_value: object = "adversary",
    method: object = "session.status",
    params: object = None,
    protocol: object = RPC_PROTOCOL_VERSION,
    drop: tuple[str, ...] = (),
) -> bytes:
    """A hand-built request frame, bypassing :func:`encode_request` on purpose.

    The format marker is a string literal because an adversary who sniffed one
    real frame has it; using the module's private constant here would quietly
    re-route these bytes through the very code they are meant to ambush.
    """
    document: dict[str, object] = {
        "format": "pz-agent-core-rpc/1",
        "protocol": protocol,
        "id": id_value,
        "method": method,
        "params": {} if params is None else params,
    }
    for key in drop:
        del document[key]
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def _ask_raw(harness: Harness, payload: bytes) -> RpcResponse:
    """Send one raw frame on a fresh authenticated connection; decode the answer.

    The ``poll`` bound is the assertion that the server *answered* rather than
    dropping the link or holding it: a refusal that arrives as a closed
    connection would fail here in ``recv_bytes``, and one that never arrives
    fails the poll rather than parking the suite.
    """
    connection = harness.raw()
    try:
        connection.send_bytes(payload)
        assert connection.poll(GRACE), "the server never answered the frame"
        answer = connection.recv_bytes()
    finally:
        connection.close()
    return decode_response(answer)


class TestAnUnknownMethodArrivingAsRawBytes:
    """(a) Semantically hostile, syntactically perfect: the frame decodes, the
    method does not exist. The failure mode being guarded against is a dispatch
    that crashes or drops the link on a name it has no entry for — the answer
    must be the typed ``UNKNOWN_METHOD`` envelope, addressed to the request
    that asked, with the server still on the air afterwards.
    """

    def test_a_hostile_method_name_gets_the_typed_refusal_not_a_crash(self, serving: Start) -> None:
        harness = serving(_router)

        reply = _ask_raw(harness, _envelope(id_value="probe-1", method="../../../etc/shadow"))

        assert reply.ok is False
        assert reply.error_code == ErrorCode.UNKNOWN_METHOD
        assert reply.id == "probe-1", "the refusal must answer the request that asked"

    def test_the_refusal_does_not_echo_the_method_and_service_continues(
        self, serving: Start
    ) -> None:
        harness = serving(_router)
        hostile = "system.exec.zZmarkerZz"
        assert len(hostile) <= MAX_METHOD_CHARS, "the probe must reach the router to count"

        reply = _ask_raw(harness, _envelope(method=hostile))

        assert reply.error_code == ErrorCode.UNKNOWN_METHOD
        assert "zZmarkerZz" not in reply.error_message
        assert harness.client().call("session.status").ok, (
            "an unknown method took the server down with it"
        )


class TestMalformedFramesAreAnsweredNotFatal:
    """(b) Bytes :func:`decode_request` refuses. The transport test proves one
    shape — raw non-JSON — gets an answer; these are the shapes *near* a valid
    envelope, which is where a decoder crash would actually live: valid JSON of
    the wrong kind, a foreign protocol's envelope, ours with a required field
    missing, ours truncated mid-frame. Every one must come back as the
    ``MALFORMED`` envelope with the documented ``"unknown"`` id — the server
    cannot echo an id it could not parse — and the server must answer an honest
    call after the whole barrage, because a decoder that dies on the first
    probe turns one hostile frame into a denial of service.
    """

    def test_a_barrage_of_near_miss_frames_each_get_the_malformed_envelope(
        self, serving: Start
    ) -> None:
        harness = serving(_router)
        barrage: list[tuple[bytes, str]] = [
            (b"[1,2,3]", "JSON, but an array"),
            (b'{"jsonrpc":"2.0","method":"x","id":1}', "somebody else's RPC envelope"),
            (_envelope(drop=("method",)), "our envelope, method missing"),
            (_envelope(drop=("id",)), "our envelope, id missing"),
            (_envelope(drop=("protocol",)), "our envelope, protocol missing"),
            (_envelope()[: len(_envelope()) // 2], "our envelope, truncated mid-frame"),
        ]

        for payload, shape in barrage:
            reply = _ask_raw(harness, payload)

            assert reply.ok is False, shape
            assert reply.error_code == ErrorCode.MALFORMED, shape
            assert reply.id == "unknown", f"{shape}: a made-up id was echoed for a frame with none"

        assert harness.client().call("session.status").ok, (
            "the malformed barrage took the server down"
        )

    def test_a_wrong_protocol_major_is_the_typed_mismatch_not_generic_garbage(
        self, serving: Start
    ) -> None:
        """An old executable talking to a new sidecar must learn *which* problem
        it has: ``PROTOCOL_MISMATCH`` means reinstall shipped two halves,
        ``MALFORMED`` means the bytes were noise, and collapsing the first into
        the second sends the user debugging the wrong thing.
        """
        harness = serving(_router)

        reply = _ask_raw(harness, _envelope(protocol="999.0"))

        assert reply.ok is False
        assert reply.error_code == ErrorCode.PROTOCOL_MISMATCH
        assert harness.client().call("session.status").ok


class TestAReplayedRequest:
    """(c) The same frame, byte for byte, over a second fresh connection.

    The documented semantics are one request per connection with no request-id
    memory in the transport, so the observed behaviour — pinned here rather
    than guessed at — is that the replay is *served again*, and served
    identically. That is deliberate, not a hole: a frame can only be delivered
    over a connection that passed the challenge, so a replayer already holds
    the per-run key, and the key dying with the run is the replay protection.
    What this test forbids is the middle ground — a dedupe cache or a nonce
    check appearing in the transport silently, changing the second answer
    without anyone re-deriving the semantics.
    """

    def test_the_same_bytes_on_a_new_connection_are_served_again_and_identically(
        self, serving: Start
    ) -> None:
        served_ids: list[str] = []

        def counting(request: RpcRequest) -> RpcResponse:
            served_ids.append(request.id)
            return RpcResponse(id=request.id, ok=True, result={"answer": 41})

        harness = serving(counting)
        frame = encode_request(RpcRequest(id="replay-1", method="observation.get", params={}))

        first = _ask_raw(harness, frame)
        second = _ask_raw(harness, frame)

        assert first.ok is True
        assert second == first, "the replayed request was answered differently"
        assert served_ids == ["replay-1", "replay-1"], (
            "one request per connection means each connection is served on its "
            "own; the transport grew request-id memory nobody documented"
        )


class TestAStaleDescriptor:
    """(d) The descriptor pipeline end to end, against a server that died.

    The transport test dials a made-up address; this one goes through the real
    artefacts a crashed sidecar leaves behind: a descriptor file naming a live
    pid (the test process), a token file still on disk, and a socket file with
    nobody behind it. ``load_descriptor`` is *supposed* to accept that set —
    the pid is alive and the token exists — so the stale case lands on the
    client, and the client must turn it into ``RpcUnavailable`` within its own
    deadline. The hang this forbids is the real user-facing one: an MCP client
    showing a spinner against a corpse.
    """

    def test_a_descriptor_whose_server_died_fails_fast_not_forever(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("the corpse socket file is an AF_UNIX artefact")
        state = tmp_path / "state"
        runtime = runtime_dir(state)
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_router)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        write_descriptor(state, server.descriptor())
        address = server.address
        server.close()
        thread.join(timeout=GRACE)
        assert not thread.is_alive()
        # `close` unlinks its socket file; a kill -9 would not. Put the corpse
        # back the way a crash leaves it: bound once, closed, never listening.
        corpse = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        corpse.bind(address)
        corpse.close()
        assert Path(address).exists(), "the stale socket file this test is about is missing"

        descriptor = load_descriptor(state)

        client = RpcClient(descriptor, authkey=read_token(runtime), deadline=2.0)
        started = time.monotonic()
        with pytest.raises(RpcUnavailable, match="not answering"):
            client.call("session.status")

        assert time.monotonic() - started < GRACE, "the stale descriptor was a hang, not an error"


class TestTheWrongLiveServer:
    """(e) Crossed installs: the descriptor names one live sidecar, the token
    came from another. Unlike the transport test's made-up key against one
    server, both ends here are real and healthy — the failure must be an auth
    refusal in the client's own words, it must arrive within the deadline, and
    neither sidecar may be harmed by having been on either side of the mix-up.
    """

    def test_a_client_with_the_other_servers_token_is_refused_within_the_deadline(
        self, serving: Start
    ) -> None:
        alpha = serving(_router)
        beta = serving(_router)
        assert alpha.key != beta.key, "two runs issued one key; nothing below means anything"

        crossed = RpcClient(
            alpha.server.descriptor(), authkey=read_token(beta.runtime), deadline=GRACE
        )
        started = time.monotonic()
        with pytest.raises(RpcUnavailable, match="token does not match"):
            crossed.call("session.status")
        elapsed = time.monotonic() - started

        assert elapsed < GRACE, f"the auth refusal took {elapsed:.1f}s"
        assert alpha.client().call("session.status").ok, "the refusal wounded the refusing server"
        assert beta.client().call("session.status").ok, "the other install was somehow involved"


class TestProtocolFieldAbuse:
    """(f) The ``id`` and ``method`` fields at pathological length or type.

    Every frame here is under the 64 KiB transport cap, so the recv layer
    passes it and the decoder is the only guard left. Each abuse must be
    refused as ``MALFORMED`` — answered, within the poll bound ``_ask_raw``
    enforces, never crashed on — and the caps must be doing the refusing:
    the same field at its exact documented maximum must go through.
    """

    def test_each_abusive_field_is_refused_and_the_server_outlives_the_barrage(
        self, serving: Start
    ) -> None:
        harness = serving(_router)
        abuses: list[tuple[bytes, str]] = [
            (_envelope(id_value="a" * 60_000), "an id of sixty thousand characters"),
            (_envelope(method="m" * 4_096), "a method of four thousand characters"),
            (_envelope(id_value=1234567), "an id that is a number"),
            (_envelope(method=["sh", "-c", "id"]), "a method that is a list"),
            (_envelope(id_value=""), "an empty id"),
            (_envelope(params="not an object"), "params that are a string"),
        ]

        for payload, abuse in abuses:
            assert len(payload) <= MAX_REQUEST_BYTES, (
                f"{abuse}: the probe overflowed the transport cap and never reached the decoder"
            )
            reply = _ask_raw(harness, payload)

            assert reply.ok is False, abuse
            assert reply.error_code == ErrorCode.MALFORMED, abuse

        assert harness.client().call("session.status").ok, "the abuse barrage killed the server"

    def test_the_refusal_names_the_length_never_the_value(self, serving: Start) -> None:
        """The wire module promises its errors never quote the payload; an id
        is attacker-chosen text headed for a log line, so the promise matters
        most exactly here.
        """
        harness = serving(_router)
        marker = "zQmarkerQz"

        over_cap_id = _ask_raw(harness, _envelope(id_value=marker * 3_000))
        over_cap_method = _ask_raw(harness, _envelope(method=marker * 10))

        assert over_cap_id.error_code == ErrorCode.MALFORMED
        assert marker not in over_cap_id.error_message, "the refusal echoed the hostile id"
        assert over_cap_method.error_code == ErrorCode.MALFORMED
        assert marker not in over_cap_method.error_message, "the refusal echoed the hostile method"

    def test_the_documented_maximums_themselves_are_still_legal(self, serving: Start) -> None:
        """The cap refuses what is *past* it. An off-by-one that also refused
        the boundary would strand a legal client, and nothing else in this
        class can tell that regression from the guard working.
        """
        harness = serving(_router)

        at_the_cap = _ask_raw(
            harness,
            _envelope(id_value="i" * MAX_ID_CHARS, method="k" * MAX_METHOD_CHARS),
        )

        assert at_the_cap.error_code == ErrorCode.UNKNOWN_METHOD, (
            "a field at its documented maximum was refused as malformed"
        )
        assert at_the_cap.id == "i" * MAX_ID_CHARS, "the legal id was not echoed back"
