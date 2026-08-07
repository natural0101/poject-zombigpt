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

import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from pz_agent_core.rpc.descriptor import FAMILY_PIPE, FAMILY_UNIX, RpcDescriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import (
    AddressTooLong,
    RpcClient,
    RpcServer,
    RpcUnavailable,
    local_family,
    new_address,
)
from pz_agent_core.rpc.wire import ErrorCode, RpcError, RpcRequest, RpcResponse

#: Long enough that a loaded CI runner does not fail the happy path, short
#: enough that a genuine hang ends the test rather than the suite's patience.
GRACE: float = 10.0

#: What the `serving` fixture hands back: start a server with this handler.
Start = Callable[[Callable[[RpcRequest], RpcResponse]], "Harness"]


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

    def start(handler: Callable[[RpcRequest], RpcResponse]) -> Harness:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=handler)
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

    def test_a_posix_address_is_inside_the_runtime_directory(self, tmp_path: Path) -> None:
        address = new_address(tmp_path, family=FAMILY_UNIX)

        assert Path(address).parent == tmp_path

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


def test_an_answer_for_a_different_request_is_refused(serving: Start) -> None:
    """One request per connection, so a mismatched id is not a reordering."""

    def liar(request: RpcRequest) -> RpcResponse:
        return RpcResponse(id="somebody-elses-id", ok=True)

    harness = serving(liar)

    with pytest.raises(RpcError, match="does not match"):
        harness.client().call("session.status")
