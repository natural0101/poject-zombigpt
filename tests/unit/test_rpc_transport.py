"""A real server and a real client, over the family this machine can bind.

Not mocked. The claims here — the wrong key is refused, a slow server hits the
deadline, a handler that raises does not take the link down, shutdown actually
finishes — are all about what the operating system does, and a double would
answer for all of them by construction.

The one thing these cannot cover is the *other* family: a Linux runner binds
``AF_UNIX`` and a Windows runner binds ``AF_PIPE``, and neither can bind the
other. That is why the descriptor and wire tests are shaped so both families are
exercised everywhere, and why this file asserts which family it is on rather
than assuming.
"""

from __future__ import annotations

import os
import secrets
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

import pytest

import pz_agent_core.rpc.transport as transport_module
from pz_agent_core.rpc.descriptor import FAMILY_PIPE, FAMILY_UNIX, RpcDescriptor
from pz_agent_core.rpc.token import MIN_TOKEN_BYTES, issue_token, read_token
from pz_agent_core.rpc.transport import (
    ACCEPT_FAILURE_BUDGET,
    IDLE_SECONDS,
    AddressTooLong,
    RpcClient,
    RpcServer,
    RpcUnavailable,
    local_family,
    new_address,
)
from pz_agent_core.rpc.wire import (
    ErrorCode,
    RpcError,
    RpcRequest,
    RpcResponse,
    encode_request,
)

#: Long enough that a loaded CI runner does not fail the happy path, short
#: enough that a genuine hang ends the test rather than the suite's patience.
GRACE: float = 10.0


class Start(Protocol):
    """What the `serving` fixture hands back: start a server with this handler."""

    def __call__(
        self,
        handler: Callable[[RpcRequest], RpcResponse],
        *,
        idle_seconds: float | None = None,
    ) -> Harness: ...


@dataclass
class Harness:
    server: RpcServer
    thread: threading.Thread
    key: bytes

    def client(self, *, deadline: float = GRACE, key: bytes | None = None) -> RpcClient:
        return RpcClient(self.server.descriptor(), authkey=key or self.key, deadline=deadline)


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[Start]:
    started: list[Harness] = []

    def start(
        handler: Callable[[RpcRequest], RpcResponse], *, idle_seconds: float | None = None
    ) -> Harness:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        # idle_seconds is left at the server default unless a test needs the
        # server to recover from an abandoned connection quickly. On a Unix
        # socket a peer that hangs up is an immediate EOF, so recovery is
        # instant either way; on a Windows named pipe a read started before the
        # peer vanished has no hard deadline (the transport documents this), so
        # the serving thread is freed only when the idle budget elapses — a
        # test that asserts the server survives such a peer must shrink that
        # budget below its own patience.
        server = RpcServer(
            new_address(runtime),
            authkey=key,
            handler=handler,
            idle_seconds=IDLE_SECONDS if idle_seconds is None else idle_seconds,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        harness = Harness(server=server, thread=thread, key=key)
        started.append(harness)
        return harness

    yield start

    for harness in started:
        harness.server.close()
        harness.thread.join(timeout=GRACE)


def _echo(request: RpcRequest) -> RpcResponse:
    return RpcResponse(id=request.id, ok=True, result={"method": request.method, **request.params})


class TestTheLinkWorks:
    def test_a_call_reaches_the_handler_and_the_answer_comes_back(self, serving: Start) -> None:
        harness = serving(_echo)

        response = harness.client().call("session.status", {"n": 3})

        assert response.ok is True
        assert response.result == {"method": "session.status", "n": 3}

    def test_many_calls_in_a_row_each_get_their_own_connection(self, serving: Start) -> None:
        """One request per connection: the second call must not see the first's socket."""
        harness = serving(_echo)
        client = harness.client()

        answers = [client.call("m", {"i": index}).result["i"] for index in range(20)]

        assert answers == list(range(20))

    def test_the_family_is_the_one_this_platform_actually_uses(self, serving: Start) -> None:
        harness = serving(_echo)

        expected = FAMILY_PIPE if sys.platform == "win32" else FAMILY_UNIX
        assert harness.server.family == expected == local_family()

    def test_the_descriptor_the_server_publishes_is_the_one_that_connects(
        self, serving: Start
    ) -> None:
        harness = serving(_echo)
        descriptor = harness.server.descriptor()

        assert RpcClient(descriptor, authkey=harness.key, deadline=GRACE).call("x").ok


class TestAuthentication:
    def test_the_key_the_server_holds_is_the_key_a_client_reads(self, tmp_path: Path) -> None:
        """The seam the token bug fell through, and nothing was watching it.

        `issue_token` returns the secret *and* writes it. The server is started
        with the returned value; a client reads the file. Every test held both
        ends of that in memory, so nobody compared the two — and on Windows they
        differed, because `os.open` defaults to text mode and turned a `0x0A`
        inside the secret into two bytes. About one token in eight contains a
        newline, so one Windows run in eight refused a connection that should
        have been fine.

        The token is forced to contain a newline rather than waited for: at one
        in eight, a random token makes this pass by luck seven times out of
        eight, which is worse than not testing it.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        forced = b"\xa1" * 7 + b"\n" + b"\xb2" * 8 + b"\r" + b"\xc3" * 15
        assert len(forced) == MIN_TOKEN_BYTES

        with mock.patch.object(secrets, "token_bytes", return_value=forced):
            issued = issue_token(runtime)

        assert issued == forced, "issue_token did not return what it was given"
        assert read_token(runtime) == issued, (
            "the file does not hold the key the server was handed; a client "
            "reading it would authenticate with a secret that was never issued"
        )

        server = RpcServer(new_address(runtime), authkey=issued, handler=_echo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from_file = read_token(runtime)
            client = RpcClient(server.descriptor(), authkey=from_file, deadline=GRACE)

            assert client.call("session.status").ok
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_the_wrong_key_cannot_call_anything(self, serving: Start) -> None:
        harness = serving(_echo)

        with pytest.raises(RpcUnavailable):
            harness.client(key=b"n" * 32, deadline=GRACE).call("session.status")

    def test_a_refused_connection_does_not_stop_the_server(self, serving: Start) -> None:
        """Otherwise one bad client is a denial of service against the good one."""
        harness = serving(_echo)

        with pytest.raises(RpcUnavailable):
            harness.client(key=b"n" * 32, deadline=GRACE).call("session.status")

        assert harness.client().call("still.here").ok

    def test_the_refusal_does_not_quote_the_key(self, serving: Start) -> None:
        harness = serving(_echo)
        wrong = b"q" * 32

        with pytest.raises(RpcUnavailable) as caught:
            harness.client(key=wrong, deadline=GRACE).call("x")

        assert wrong.hex() not in str(caught.value)
        assert harness.key.hex() not in str(caught.value)


class TestFailureIsAnAnswer:
    def test_a_handler_that_raises_answers_rather_than_dropping_the_link(
        self, serving: Start
    ) -> None:
        def explode(request: RpcRequest) -> RpcResponse:
            raise RuntimeError("the core refused")

        harness = serving(explode)

        response = harness.client().call("session.arm")

        assert response.ok is False
        assert response.error_code == ErrorCode.CORE_REFUSED
        assert "the core refused" in response.error_message

    def test_the_server_survives_a_handler_that_raises(self, serving: Start) -> None:
        calls: list[str] = []

        def sometimes(request: RpcRequest) -> RpcResponse:
            calls.append(request.method)
            if request.method == "boom":
                raise RuntimeError("no")
            return RpcResponse(id=request.id, ok=True)

        harness = serving(sometimes)
        client = harness.client()

        assert client.call("boom").ok is False
        assert client.call("fine").ok is True
        assert calls == ["boom", "fine"]

    def test_a_malformed_request_is_answered_rather_than_ignored(self, serving: Start) -> None:
        """A client that sent nonsense gets told so; it must not just hang."""
        from multiprocessing.connection import Client  # noqa: PLC0415

        harness = serving(_echo)
        family = "AF_PIPE" if harness.server.family == FAMILY_PIPE else "AF_UNIX"
        connection = Client(harness.server.address, family=family, authkey=harness.key)
        try:
            connection.send_bytes(b"not json at all")
            assert connection.poll(GRACE), "the server never answered a malformed request"
            answer = connection.recv_bytes()
        finally:
            connection.close()

        from pz_agent_core.rpc.wire import decode_response  # noqa: PLC0415

        response = decode_response(answer)
        assert response.ok is False
        assert response.error_code == ErrorCode.MALFORMED


class TestTheDeadline:
    def test_a_server_that_does_not_answer_in_time_is_given_up_on(self, serving: Start) -> None:
        """An MCP client with a hung tool call shows a spinner and no way out."""
        release = threading.Event()

        def stall(request: RpcRequest) -> RpcResponse:
            release.wait(GRACE)
            return RpcResponse(id=request.id, ok=True)

        harness = serving(stall)
        started = time.monotonic()
        try:
            with pytest.raises(RpcUnavailable, match="no answer within"):
                harness.client(deadline=0.5).call("slow")
            elapsed = time.monotonic() - started
        finally:
            release.set()

        assert elapsed < GRACE, f"the deadline did not fire; waited {elapsed:.1f}s"

    def test_a_deadline_of_zero_is_a_programming_error(self, tmp_path: Path) -> None:
        descriptor = RpcDescriptor(address="/tmp/x", family=FAMILY_UNIX, pid=1)

        with pytest.raises(ValueError, match="deadline"):
            RpcClient(descriptor, authkey=b"k" * 32, deadline=0)

    def test_an_address_nothing_is_listening_on_is_unavailable_not_a_hang(
        self, tmp_path: Path
    ) -> None:
        address = (
            r"\\.\pipe\pz-agent-nothing-here"
            if local_family() == FAMILY_PIPE
            else str(tmp_path / "absent.sock")
        )
        client = RpcClient(
            RpcDescriptor(address=address, family=local_family(), pid=1),
            authkey=b"k" * 32,
            deadline=GRACE,
        )

        started = time.monotonic()
        with pytest.raises(RpcUnavailable):
            client.call("session.status")

        assert time.monotonic() - started < GRACE


class TestShutdown:
    def test_close_ends_the_serving_thread(self, tmp_path: Path) -> None:
        """`close` used to return while the thread stayed parked in `accept`.

        Closing a listening socket does not wake a thread blocked accepting on
        it, so the sidecar's shutdown returned and the process never exited.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("warm")

        server.close()
        thread.join(timeout=GRACE)

        assert not thread.is_alive(), "serve_forever did not return after close"

    def test_closing_twice_is_not_an_error(self, tmp_path: Path) -> None:
        """Shutdown runs on paths that may already have run it."""
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        server = RpcServer(new_address(runtime), authkey=issue_token(runtime), handler=_echo)

        server.close()
        server.close()

    @pytest.mark.skipif(sys.platform == "win32", reason="a named pipe is not a file")
    def test_the_socket_file_does_not_outlive_the_server(self, tmp_path: Path) -> None:
        """A leftover socket makes the next start fail on an address in use by nobody."""
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        server = RpcServer(new_address(runtime), authkey=issue_token(runtime), handler=_echo)
        address = Path(server.address)
        assert address.exists()

        server.close()

        assert not address.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="a named pipe is not a file")
    def test_a_second_server_can_bind_after_the_first_stops(self, tmp_path: Path) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        first = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        first.close()

        second = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        try:
            thread = threading.Thread(target=second.serve_forever, daemon=True)
            thread.start()
            assert RpcClient(second.descriptor(), authkey=key, deadline=GRACE).call("x").ok
        finally:
            second.close()

    def test_a_client_calling_a_stopped_server_is_told_so(self, serving: Start) -> None:
        harness = serving(_echo)
        client = harness.client(deadline=2.0)
        assert client.call("warm").ok

        harness.server.close()
        harness.thread.join(timeout=GRACE)

        with pytest.raises(RpcUnavailable):
            client.call("cold")


class TestAcceptTimeFailures:
    """The accept itself failing is a different event from shutdown.

    On a Unix socket a rude client fails *after* accept — the challenge or the
    read sees the hang-up — but a Windows named pipe can surface the same
    client as an ``OSError`` out of ``accept`` itself, and treating every
    accept-time ``OSError`` as "``close`` was called" stopped the whole
    sidecar for one bad peer. The distinction the code must draw is the stop
    flag, which ``close`` sets before it touches the listener; and because
    "keep serving" would spin hot on a listener whose accept fails instantly
    forever, survival is bounded by a budget of consecutive failures. Both
    halves are observed here on the real family this machine binds, with the
    failure injected at the exact seam — the listener's ``accept``.
    """

    def test_one_connection_level_error_out_of_accept_leaves_the_server_serving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One rude client at accept time must not be a shutdown.

        The listener's ``accept`` raises a connection-level ``OSError`` once —
        the shape a Windows named pipe gives a peer that connects and vanishes
        mid-accept — and then delegates to the real accept. Under the old
        blanket ``return False`` the serving thread ends on that first raise
        and the follow-up call finds nobody; with the flag drawing the line,
        the follow-up call must succeed.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        real_accept = server._listener.accept
        tripped = threading.Event()

        def accept_rudely_once() -> Connection:
            if not tripped.is_set():
                tripped.set()
                raise ConnectionAbortedError("a peer connected and vanished mid-accept")
            return real_accept()

        monkeypatch.setattr(server._listener, "accept", accept_rudely_once)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("after")
        finally:
            server.close()
            thread.join(timeout=GRACE)

        assert tripped.is_set(), "the fake never raised, so nothing was proven"
        assert response.ok, "one accept-time error took the sidecar down with it"
        assert not thread.is_alive(), "serve_forever did not return after close"

    def test_a_listener_that_cannot_accept_at_all_stops_within_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead listener must stop the server, boundedly, not spin it hot.

        Every ``accept`` raises instantly and nothing has asked the server to
        stop, so "keep serving" with no bound would burn a core forever. The
        loop must give up on its own: ``serve_forever`` returns within the
        test's patience, and the attempt count shows it spent exactly the
        budget of consecutive failures — no fewer (it did try) and no more
        (it did not spin past its own limit).
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        attempts: list[int] = []

        def dead_accept() -> Connection:
            attempts.append(1)
            raise OSError("the listener is dead and every accept fails at once")

        monkeypatch.setattr(server._listener, "accept", dead_accept)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        started = time.monotonic()
        thread.start()
        try:
            thread.join(timeout=GRACE)
            elapsed = time.monotonic() - started

            assert not thread.is_alive(), (
                f"serve_forever kept spinning on a dead listener past {GRACE:g}s"
            )
            assert len(attempts) == ACCEPT_FAILURE_BUDGET, (
                f"expected exactly the budget of {ACCEPT_FAILURE_BUDGET} consecutive "
                f"attempts, saw {len(attempts)}"
            )
            assert elapsed < GRACE, f"stopping took {elapsed:.1f}s"
        finally:
            server.close()

    def test_a_successful_accept_resets_the_failure_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consecutive is the load-bearing word: a served client wipes the slate.

        A listener that alternates bursts of failures with real connections is
        alive, however long it runs; only an unbroken run of failures means
        the listener is dead. The fake raises one failure short of the budget,
        hands over a real connection — which must reset the counter — and
        re-arms the next burst *inside itself*, before returning, so no test
        thread races the serving loop back into ``accept``. Two such bursts
        total more than the budget: a counter that merely accumulated across
        successes would cross it during the second burst and the second call
        would find a stopped server.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        real_accept = server._listener.accept
        remaining = [ACCEPT_FAILURE_BUDGET - 1]

        def accept_in_bursts() -> Connection:
            if remaining[0] > 0:
                remaining[0] -= 1
                raise ConnectionResetError("a rude client in a burst of them")
            connection = real_accept()
            remaining[0] = ACCEPT_FAILURE_BUDGET - 1
            return connection

        monkeypatch.setattr(server._listener, "accept", accept_in_bursts)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RpcClient(server.descriptor(), authkey=key, deadline=GRACE)
            assert client.call("first").ok, "the first burst alone stopped the server"
            assert client.call("second").ok, (
                "the budget accumulated across a successful accept instead of resetting"
            )
        finally:
            server.close()
            thread.join(timeout=GRACE)

        assert not thread.is_alive(), "serve_forever did not return after close"


class TestNothingIsPickled:
    def test_the_server_never_calls_the_pickling_receive(
        self, serving: Start, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`recv` is one letter from `recv_bytes` and pickles what it reads.

        Poisoning it means the test fails loudly if the wiring ever reaches for
        the convenient call, instead of shipping a process that will execute
        whatever is written to its pipe.
        """
        from multiprocessing.connection import Connection  # noqa: PLC0415

        def forbidden(self: object, *args: object, **kwargs: object) -> object:
            raise AssertionError("the transport unpickled a message")

        monkeypatch.setattr(Connection, "recv", forbidden)
        monkeypatch.setattr(Connection, "send", forbidden)

        harness = serving(_echo)

        assert harness.client().call("session.status").ok


class TestAddresses:
    def test_two_servers_do_not_collide_on_one_address(self, tmp_path: Path) -> None:
        """On Windows a named pipe name is global, so two accounts would share it."""
        first = new_address(tmp_path / "a")
        second = new_address(tmp_path / "b")

        if local_family() == FAMILY_PIPE:
            assert first != second
        else:
            assert Path(first).parent != Path(second).parent

    def test_a_windows_address_is_a_pipe_name_rather_than_a_path(self, tmp_path: Path) -> None:
        """Checkable from Linux, which is where this defect would otherwise hide."""
        address = new_address(tmp_path, family=FAMILY_PIPE)

        assert address.startswith("\\\\.\\pipe\\")
        assert str(tmp_path) not in address, "the pipe name leaked the state directory"

    def test_a_posix_address_is_inside_the_runtime_directory(self) -> None:
        """A fixed short root, deliberately not `tmp_path`.

        The claim here is about how the address is *composed*, and `tmp_path` on
        a Windows runner is 116 bytes — past the `sun_path` limit — so asking for
        a POSIX address under it correctly refuses, and this test failed for a
        reason that had nothing to do with what it was checking. The length rule
        has its own test below; this one must not depend on where pytest happens
        to put its temporary directories.
        """
        root = Path("/srv/pz/runtime")

        address = new_address(root, family=FAMILY_UNIX)

        assert Path(address).parent == root
        assert Path(address).name.endswith(".sock")

    def test_a_posix_address_that_will_not_bind_says_so_before_it_tries(
        self, tmp_path: Path
    ) -> None:
        """`sun_path` is 108 bytes on Linux and 104 on macOS, terminator included.

        Past that, `bind` fails from inside the socket layer with an error about
        a filename, which reads like a missing directory rather than a path
        thirty characters too long. Windows has no such limit — this is the one
        way the POSIX family is more constrained than the shipped one.
        """
        deep = tmp_path / ("Пользователь" * 12) / "Zomboid" / "pz-agent" / "runtime"

        with pytest.raises(AddressTooLong, match="limit is"):
            new_address(deep, family=FAMILY_UNIX)

    def test_that_refusal_does_not_quote_the_path(self, tmp_path: Path) -> None:
        """The path runs through the profile directory, so it carries the account name."""
        deep = tmp_path / ("Иван" * 40) / "runtime"

        with pytest.raises(AddressTooLong) as caught:
            new_address(deep, family=FAMILY_UNIX)

        assert "Иван" not in str(caught.value)
        assert str(tmp_path) not in str(caught.value)

    def test_a_realistic_state_directory_is_comfortably_inside_the_limit(self) -> None:
        """`~/Zomboid/pz-agent` with a Cyrillic account name, which is the real case."""
        address = new_address(
            Path("/home/Пользователь/Zomboid/pz-agent/runtime"), family=FAMILY_UNIX
        )

        assert len(address.encode("utf-8")) < 100, address


def test_an_unknown_method_is_the_handlers_business_not_the_transports(serving: Start) -> None:
    """The transport routes bytes; it does not know the method catalogue."""

    def router(request: RpcRequest) -> RpcResponse:
        if request.method != "known":
            return RpcResponse(
                id=request.id,
                ok=False,
                error_code=ErrorCode.UNKNOWN_METHOD,
                error_message=f"no method {request.method}",
            )
        return RpcResponse(id=request.id, ok=True)

    harness = serving(router)

    assert harness.client().call("known").ok is True
    refused = harness.client().call("invented")
    assert refused.ok is False
    assert refused.error_code == ErrorCode.UNKNOWN_METHOD


class TestEveryWaitOnThePeerIsBounded:
    """AGENTS.md: bounded everything — observed here, not assumed.

    The criterion audit found the waits below either unbounded or unwatched.
    Each test is failing-capable: remove the bound it observes and the fake
    peer holds the wait past GRACE, which the elapsed assertions catch. The
    fakes that need a raw file descriptor are AF_UNIX sockets, so those skip
    on Windows; what Windows genuinely cannot bound is pinned by the
    docstring test at the end rather than left silently untested.
    """

    def test_a_peer_that_trickles_the_payload_hits_the_deadline(self, tmp_path: Path) -> None:
        """``poll`` passes on the first byte; the bound must hold for the rest.

        The fake authenticates honestly, then sends a frame header claiming a
        megabyte, sixteen bytes of payload, and nothing more, holding the
        connection open. Every per-poll check is satisfied, so only a real
        deadline on the read itself can end this call.
        """
        if sys.platform == "win32":
            pytest.skip("the fake trickling peer is an AF_UNIX socket")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = secrets.token_bytes(32)
        address = new_address(runtime)
        listener = Listener(address, family="AF_UNIX", authkey=key)
        hold = threading.Event()

        def trickle() -> None:
            with suppress(Exception), closing(listener.accept()) as victim:
                os.write(victim.fileno(), struct.pack("!i", 1_000_000) + b"x" * 16)
                hold.wait(GRACE * 3)

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        client = RpcClient(
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
            authkey=key,
            deadline=1.0,
        )
        started = time.monotonic()
        try:
            with pytest.raises(RpcUnavailable, match="no answer within"):
                client.call("session.status")
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            listener.close()
            thread.join(timeout=GRACE)

        assert elapsed < GRACE, f"the read outlived the deadline; waited {elapsed:.1f}s"

    def test_a_peer_that_accepts_and_never_authenticates_is_given_up_on(
        self, tmp_path: Path
    ) -> None:
        """The stdlib handshake would block in ``recv`` forever here; the guard must not."""
        if sys.platform == "win32":
            pytest.skip("the fake mute peer is an AF_UNIX socket")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        address = new_address(runtime)
        mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mute.bind(address)
        mute.listen(1)
        taken: list[socket.socket] = []
        hold = threading.Event()

        def swallow() -> None:
            with suppress(OSError):
                accepted, _ = mute.accept()
                taken.append(accepted)
                hold.wait(GRACE * 3)

        thread = threading.Thread(target=swallow, daemon=True)
        thread.start()
        client = RpcClient(
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
            authkey=b"k" * 32,
            deadline=1.0,
        )
        started = time.monotonic()
        try:
            with pytest.raises(RpcUnavailable, match="handshake"):
                client.call("session.status")
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            mute.close()
            thread.join(timeout=GRACE)
            for accepted in taken:
                accepted.close()

        assert elapsed < GRACE, f"the handshake outlived the deadline; waited {elapsed:.1f}s"

    def test_an_authenticated_connection_that_sends_nothing_is_dropped_after_the_idle_budget(
        self, tmp_path: Path
    ) -> None:
        """Shrunk via injection; the mechanism is the same poll that defaults to a minute.

        Change the server's idle wait to ``poll(None)`` and this fails: the
        drop never comes, ``_dropped_within`` runs out of GRACE, and the
        elapsed floor separately proves the drop is the budget expiring
        rather than the connection being refused outright.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo, idle_seconds=0.5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            family = "AF_PIPE" if server.family == FAMILY_PIPE else "AF_UNIX"
            connection = Client(server.address, family=family, authkey=key)
            started = time.monotonic()
            with closing(connection):
                dropped = _dropped_within(connection, GRACE)
            elapsed = time.monotonic() - started

            assert dropped, "the idle connection was never dropped"
            assert elapsed < GRACE, f"the drop took {elapsed:.1f}s"
            assert elapsed >= 0.3, "dropped before the idle budget rather than after it"
            assert RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("after").ok
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_a_peer_that_connects_and_never_speaks_cannot_hold_the_server(
        self, tmp_path: Path
    ) -> None:
        """The challenge runs outside ``accept`` now, against the idle budget.

        The stdlib runs it inside ``accept`` with unbounded reads, so a peer
        that connects and stays silent would wedge the accept loop — and with
        it every later client — forever.
        """
        if sys.platform == "win32":
            pytest.skip("the mute peer is a raw AF_UNIX connect")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo, idle_seconds=0.5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            started = time.monotonic()
            with closing(mute):
                mute.settimeout(GRACE)
                mute.connect(server.address)
                while True:
                    try:
                        chunk = mute.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
            elapsed = time.monotonic() - started

            assert elapsed < GRACE, f"the server humoured the mute peer for {elapsed:.1f}s"
            assert RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("after").ok
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_an_over_cap_request_frame_is_refused_without_reading_a_payload(
        self, tmp_path: Path
    ) -> None:
        """``recv_bytes(maxlength=...)`` refuses at the header, before any allocation.

        Only the four header bytes are ever sent, so the refusal cannot have
        come from reading the claimed gigabyte. The idle budget is left at
        its one-minute default, so a drop inside half of GRACE cannot be the
        idle path either — delete the ``maxlength`` and this fails, because
        the server then sits waiting for a payload that never comes.
        """
        if sys.platform == "win32":
            pytest.skip("raw frame injection needs a socket file descriptor")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = Client(server.address, family="AF_UNIX", authkey=key)
            started = time.monotonic()
            with closing(connection):
                os.write(connection.fileno(), struct.pack("!i", 1 << 30))
                dropped = _dropped_within(connection, GRACE)
            elapsed = time.monotonic() - started

            assert dropped, "the server accepted a frame claiming a gigabyte"
            assert elapsed < GRACE / 2, f"the refusal was not at the recv layer ({elapsed:.1f}s)"
            assert RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("after").ok
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_an_over_cap_answer_frame_is_refused_without_reading_a_payload(
        self, tmp_path: Path
    ) -> None:
        """The client's ``maxlength`` cap, observed from the other direction."""
        if sys.platform == "win32":
            pytest.skip("the fake over-claiming peer is an AF_UNIX socket")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = secrets.token_bytes(32)
        address = new_address(runtime)
        listener = Listener(address, family="AF_UNIX", authkey=key)
        hold = threading.Event()

        def overclaim() -> None:
            with suppress(Exception), closing(listener.accept()) as victim:
                os.write(victim.fileno(), struct.pack("!i", 1 << 30))
                hold.wait(GRACE * 3)

        thread = threading.Thread(target=overclaim, daemon=True)
        thread.start()
        client = RpcClient(
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
            authkey=key,
            deadline=GRACE,
        )
        started = time.monotonic()
        try:
            with pytest.raises(RpcUnavailable, match="bad message length"):
                client.call("session.status")
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            listener.close()
            thread.join(timeout=GRACE)

        assert elapsed < GRACE / 2, f"the refusal was not at the recv layer ({elapsed:.1f}s)"

    def test_the_poll_guard_alone_bounds_the_handshake_when_no_watchdog_can_cut(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipe family's only handshake bound, exercised on the family we have.

        On Windows :func:`_cut_at` yields without arming anything, so the poll
        guard in ``_Guarded`` is the one thing standing between a mute peer
        and an unbounded handshake — and on this platform the watchdog would
        mask its removal. Replace ``_cut_at`` with exactly the shape its
        ``win32`` branch has and the guard must still end the wait: delete the
        guard's poll and this test fails instead of the watchdog covering for
        it.
        """
        if sys.platform == "win32":
            pytest.skip("the fake mute peer is an AF_UNIX socket")

        @contextmanager
        def pipe_shaped_cut(
            connection: Connection, deadline_at: float
        ) -> Iterator[threading.Event]:
            yield threading.Event()

        monkeypatch.setattr(transport_module, "_cut_at", pipe_shaped_cut)
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        address = new_address(runtime)
        mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mute.bind(address)
        mute.listen(1)
        taken: list[socket.socket] = []
        hold = threading.Event()

        def swallow() -> None:
            with suppress(OSError):
                accepted, _ = mute.accept()
                taken.append(accepted)
                hold.wait(GRACE * 3)
                # An unbounded wait then fails as "closed the connection" at
                # the hold's end instead of parking the whole suite.
                accepted.close()

        thread = threading.Thread(target=swallow, daemon=True)
        thread.start()
        client = RpcClient(
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
            authkey=b"k" * 32,
            deadline=1.0,
        )
        started = time.monotonic()
        try:
            with pytest.raises(RpcUnavailable, match="handshake"):
                client.call("session.status")
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            mute.close()
            thread.join(timeout=GRACE)
            for accepted in taken:
                accepted.close()

        assert elapsed < GRACE, f"the poll guard did not bound the wait; waited {elapsed:.1f}s"

    def test_a_peer_that_trickles_a_request_frame_is_cut_at_the_idle_budget(
        self, tmp_path: Path
    ) -> None:
        """The server-side twin of the client trickle: ``_cut_at`` around the read.

        The peer authenticates honestly, sends an under-cap frame header and
        sixteen bytes of the payload, and holds. The server's ``poll`` is
        satisfied and the cap is not exceeded, so only the watchdog around
        ``recv_bytes`` can end the read — delete it and the single serving
        thread blocks for as long as the peer cares to hold, which the
        follow-up call at the end then observes.
        """
        if sys.platform == "win32":
            pytest.skip("raw frame injection needs a socket file descriptor")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo, idle_seconds=0.5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = Client(server.address, family="AF_UNIX", authkey=key)
            started = time.monotonic()
            with closing(connection):
                os.write(connection.fileno(), struct.pack("!i", 32_768) + b"x" * 16)
                dropped = _dropped_within(connection, GRACE)
            elapsed = time.monotonic() - started

            assert dropped, "the mid-frame stall was never cut"
            assert elapsed < GRACE, f"the cut took {elapsed:.1f}s"
            assert elapsed >= 0.3, "dropped before the idle budget rather than after it"
            assert RpcClient(server.descriptor(), authkey=key, deadline=GRACE).call("after").ok
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_a_peer_that_never_reads_its_answer_cannot_hold_the_server(
        self, tmp_path: Path
    ) -> None:
        """``_reply``'s watchdog: an unread answer must not park the serving thread.

        The answer is made bigger than every buffer between the processes, so
        the server's ``send_bytes`` genuinely blocks once the peer stops
        reading. Delete the ``_cut_at`` in ``_reply`` and the single serving
        thread stays parked in the send for as long as the peer holds the
        connection — which the follow-up call then observes as its own
        deadline error.
        """
        if sys.platform == "win32":
            pytest.skip("the non-reading peer holds an AF_UNIX socket")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)

        def wide(request: RpcRequest) -> RpcResponse:
            return RpcResponse(id=request.id, ok=True, result={"blob": "x" * 3_000_000})

        server = RpcServer(new_address(runtime), authkey=key, handler=wide, idle_seconds=0.5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = Client(server.address, family="AF_UNIX", authkey=key)
            with closing(connection):
                connection.send_bytes(encode_request(RpcRequest(id="r1", method="wide", params={})))
                # Never read the 3 MB answer: the server's send fills the
                # socket buffers and blocks until its watchdog cuts the link.
                after = RpcClient(server.descriptor(), authkey=key, deadline=GRACE)
                assert after.call("after").ok, "the unread reply held the serving thread"
        finally:
            server.close()
            thread.join(timeout=GRACE)

    def test_a_non_positive_idle_budget_is_a_programming_error(self, tmp_path: Path) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)

        with pytest.raises(ValueError, match="idle_seconds"):
            RpcServer(new_address(runtime), authkey=b"k" * 32, handler=_echo, idle_seconds=0)

    def test_the_pipe_familys_weaker_read_bound_is_documented_not_denied(self) -> None:
        """Where the bound cannot hold, the module must say so, in those words.

        On a Windows named pipe there is no socket to shut down under a
        blocked read, so a read that has started cannot be given a hard
        deadline through ``multiprocessing``'s public surface. The rule from
        the audit is: never claim a bound that does not hold. This pins the
        claim the module actually makes, so a rewrite that silently drops the
        caveat — or silently drops the poll guard the caveat leans on — fails
        here.
        """
        doc = " ".join((transport_module.__doc__ or "").split())

        assert "no hard deadline on Windows" in doc
        assert "poll guard" in doc
        assert "never reads its answer" in doc, "the write-side residue lost its mention"


class TestAnEstablishedLinkDroppedMidExchange:
    """E12-M03-T003: the link drops after authentication; the client reports it
    and the sidecar keeps serving.

    Every earlier failure test breaks the link *before* it is established — a
    wrong key, a mute peer, a server that never answers in time. These three
    drop a connection that authenticated honestly, one per recovery path: the
    client's closed-connection guard around its send and read, the server's
    recv-drop guard in ``_exchange``, and the server's send suppression in
    ``_reply``. Delete any one of those and its test here fails — either the
    client surfaces a raw ``EOFError`` instead of the transport's own answer,
    or the single serving thread dies with the first rude peer and the
    follow-up call finds nobody listening.
    """

    def test_a_server_that_hangs_up_after_the_request_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        """The client's half: mid-call, after auth and send, the peer vanishes.

        The fake authenticates honestly and reads the whole request, so the
        drop lands between the client's ``send_bytes`` and its answer — the
        exact window transport.py's "closed the connection" guard covers. What
        must come back is ``RpcUnavailable`` in the transport's own words, not
        the bare ``EOFError`` the socket produced.
        """
        if sys.platform == "win32":
            pytest.skip("the fake dropping peer is an AF_UNIX listener")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = secrets.token_bytes(32)
        address = new_address(runtime)
        listener = Listener(address, family="AF_UNIX", authkey=key)

        def hang_up() -> None:
            with suppress(Exception), closing(listener.accept()) as victim:
                victim.recv_bytes()
                # The request arrived; the connection is dropped without an
                # answer when `closing` runs.

        thread = threading.Thread(target=hang_up, daemon=True)
        thread.start()
        client = RpcClient(
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
            authkey=key,
            deadline=GRACE,
        )
        try:
            with pytest.raises(RpcUnavailable, match="closed the connection"):
                client.call("session.status")
        finally:
            listener.close()
            thread.join(timeout=GRACE)

    def test_a_client_that_vanishes_after_authenticating_leaves_the_server_serving(
        self, serving: Start
    ) -> None:
        """The server's receive half: an authenticated peer hangs up unasked.

        The drop lands in ``_exchange``: the peer passed the challenge, so the
        server is past ``_authenticate`` and waiting for a request that will
        never come — its read ends in the EOF the recv-drop guard exists for.
        Without that guard the EOFError unwinds ``serve_forever`` and the
        follow-up call finds a dead sidecar.
        """
        # A short idle budget so the abandoned read frees the serving thread
        # within this test's patience: on a Unix socket the peer's close is an
        # immediate EOF, but on a Windows named pipe the read that was already
        # waiting has no hard deadline (the transport documents this asymmetry),
        # so the thread is freed only when the idle budget elapses.
        harness = serving(_echo, idle_seconds=0.5)
        family = "AF_PIPE" if harness.server.family == FAMILY_PIPE else "AF_UNIX"

        connection = Client(harness.server.address, family=family, authkey=harness.key)
        connection.close()

        assert harness.client().call("after").ok, (
            "one client hanging up mid-exchange took the sidecar down with it"
        )

    def test_a_client_that_hangs_up_before_its_answer_leaves_the_server_serving(
        self, serving: Start
    ) -> None:
        """The server's send half: the peer asked, then hung up before reading.

        The handler holds its answer until the test has really closed the
        connection, so the server's ``send_bytes`` in ``_reply`` runs against a
        peer that is guaranteed gone and fails with a broken pipe. Dropping
        that answer is the smaller loss; without the suppression the send error
        unwinds the serving thread and the follow-up call finds nobody.
        """
        gone = threading.Event()

        def hold_until_the_drop(request: RpcRequest) -> RpcResponse:
            assert gone.wait(GRACE), "the test never closed its connection"
            return RpcResponse(id=request.id, ok=True)

        harness = serving(hold_until_the_drop, idle_seconds=0.5)
        family = "AF_PIPE" if harness.server.family == FAMILY_PIPE else "AF_UNIX"

        connection = Client(harness.server.address, family=family, authkey=harness.key)
        with closing(connection):
            connection.send_bytes(encode_request(RpcRequest(id="r1", method="drop", params={})))
        gone.set()

        assert harness.client().call("after").ok, (
            "an unreadable reply took the sidecar down with it"
        )

    def test_a_drop_that_surfaces_from_the_idle_poll_is_the_same_drop(self, tmp_path: Path) -> None:
        """The Windows spelling of the vanish above, pinned at its seam.

        On a Unix socket a vanished peer answers the idle poll and the recv
        raises ``EOFError``, which the recv-drop guard catches — that is the
        path the test above exercises for real. On a Windows named pipe the
        same hang-up surfaces from ``poll`` itself as ``BrokenPipeError``,
        and with the poll outside the guard that spelling unwound
        ``serve_forever``: one abandoned client, a dead sidecar, and the
        follow-up caller timing out against a link nobody served. A real
        Unix socket cannot be made to raise from ``poll``, so the seam is
        exercised directly: a connection whose ``poll`` raises the Windows
        spelling must be absorbed by ``_exchange`` exactly like the Unix one.
        """

        class _VanishedPipe:
            """The two calls ``_exchange`` may make against a gone peer."""

            def poll(self, timeout: float | None = None) -> bool:
                raise BrokenPipeError("the pipe has been ended, as Windows spells a hang-up")

            def recv_bytes(self, maxlength: int | None = None) -> bytes:
                raise AssertionError("a poll that raised must not be followed by a read")

        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        server = RpcServer(new_address(runtime), authkey=issue_token(runtime), handler=_echo)
        try:
            server._exchange(cast(Connection, _VanishedPipe()))
        finally:
            server.close()


def _dropped_within(connection: Connection, budget: float) -> bool:
    """Whether the peer closes *connection* within *budget* seconds."""
    try:
        if not connection.poll(budget):
            return False
        connection.recv_bytes()
    except (OSError, EOFError):
        return True
    return False


def test_an_answer_for_a_different_request_is_refused(serving: Start) -> None:
    """One request per connection, so a mismatched id is not a reordering."""

    def liar(request: RpcRequest) -> RpcResponse:
        return RpcResponse(id="somebody-elses-id", ok=True)

    harness = serving(liar)

    with pytest.raises(RpcError, match="does not match"):
        harness.client().call("session.status")
