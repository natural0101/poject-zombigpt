"""The Core RPC server's lifetime inside the sidecar's, and its absence from the panic path.

Three claims, and none of them can be made against a double.

**It comes up for real.** ``SidecarRpc.start`` is asserted the way the MCP
executable actually uses it: read the descriptor from disk, read the token from
disk, dial the address they name, and get an answer back from the handler. A
test that compared the descriptor it wrote against the descriptor it held in
memory would pass against a server that never bound.

**It goes away completely.** The pass criterion is "after ``pz-agent stop``,
neither file exists", so both are asserted absent *and* the address they named
is asserted unreachable — a descriptor removed while the socket still answers
would satisfy the first half and leave a running server nobody can find.

**The panic stop never waits on it.** The server here does not answer: its
handler parks on an event the test never sets, and the test proves the wedge
before it does anything else, by watching two client calls fail to return. The
panic latch then goes down, and the assertion is a wall-clock bound. That bound
is the whole test: if the panic path ever consulted the RPC server it would wait
the client deadline (ten seconds) or the join bound (five), and two seconds
separates those from the two file writes the path actually performs.

``join_timeout_s`` is shortened in these tests and nowhere else. The shipped
bound is :data:`~pz_agent_cli.supervisor.RPC_JOIN_TIMEOUT_S`; a test asserting
that shutdown is bounded must not sit through the whole of it to find out.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.supervisor import (
    DEFAULT_STALE_AFTER_MS,
    RPC_JOIN_TIMEOUT_S,
    ControlKind,
    RpcEndpoint,
    SidecarRpc,
    SidecarSupervisor,
    SupervisorError,
    unpublish_rpc,
)
from pz_agent_cli.voice import ExchangeSessionPort
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    FAMILY_PIPE,
    FAMILY_UNIX,
    RUNTIME_DIRNAME,
    RpcDescriptor,
    StaleDescriptor,
    load_descriptor,
)
from pz_agent_core.rpc.token import MIN_TOKEN_BYTES, TOKEN_FILENAME, read_token
from pz_agent_core.rpc.transport import RpcClient, RpcUnavailable, local_family, new_address
from pz_agent_core.rpc.wire import RpcError, RpcRequest, RpcResponse
from tests.fixtures.ipc_builders import FakeClock

#: Long enough that a loaded runner does not fail a healthy call, short enough
#: that a genuine hang ends the test rather than the suite's patience.
GRACE: Final = 10.0

#: What a panic stop may cost while the RPC server is wedged. Two file writes
#: and two heartbeat reads; the numbers it has to stay clear of are the client
#: deadline (10 s) and the shutdown join (5 s), so this discriminates.
PANIC_BOUND_S: Final = 2.0

#: The join a wedged handler gets in these tests. Short on purpose — see the
#: module docstring — and asserted to be shorter than the shipped bound so this
#: constant cannot quietly become the real one.
TEST_JOIN_S: Final = 0.5

#: How long a test waits for something another thread is about to do.
SETTLE_S: Final = 2.0

#: How long a test watches a call that must *not* finish. Long enough that a
#: server which was merely slow would have answered.
WEDGE_WATCH_S: Final = 1.0


def echo(request: RpcRequest) -> RpcResponse:
    """A handler that answers, so "the server is up" means something."""
    return RpcResponse(id=request.id, ok=True, result={"method": request.method, **request.params})


@dataclass
class WedgedHandler:
    """A handler that genuinely does not answer, and says when it was entered.

    Not a sleep: a sleep is a slow server, and "slow" is what a deadline is for.
    This one parks until the fixture releases it, which is what a handler
    waiting on a lock the game is holding looks like from outside.
    """

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def __call__(self, request: RpcRequest) -> RpcResponse:
        self.entered.set()
        # Bounded even so: the fixture releases it, and this bound means a test
        # that forgot to could not leave a thread parked for the whole session.
        self.release.wait(timeout=GRACE)
        return RpcResponse(id=request.id, ok=True, result={})


@dataclass
class Started:
    """A running endpoint, and the only way its own test is allowed to dial it."""

    rpc: SidecarRpc
    endpoint: RpcEndpoint
    state_dir: Path

    def client(self, *, deadline: float = GRACE) -> RpcClient:
        """A client that finds the server the way a second process has to.

        Both halves come off disk: the descriptor from
        ``<state-dir>/runtime/core-rpc.json`` and the key from the file beside
        it. Handing the client the descriptor object the server returned would
        prove the server bound; it would not prove that what was *published* is
        what connects, which is the whole of T004.
        """
        return RpcClient(
            load_descriptor(self.state_dir),
            authkey=read_token(self.state_dir / RUNTIME_DIRNAME),
            deadline=deadline,
        )


#: ``handler -> Started``. The fixture below hands one of these to each test.
Start = Callable[..., Started]


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Start]:
    """Start endpoints, and close every one of them however the test ends."""
    made: list[SidecarRpc] = []

    def _start(
        handler: Callable[[RpcRequest], RpcResponse],
        *,
        state_dir: Path | None = None,
        join_timeout_s: float = TEST_JOIN_S,
    ) -> Started:
        directory = tmp_path / "state" if state_dir is None else state_dir
        rpc = SidecarRpc(state_dir=directory, handler=handler, join_timeout_s=join_timeout_s)
        endpoint = rpc.start()
        made.append(rpc)
        return Started(rpc=rpc, endpoint=endpoint, state_dir=directory)

    yield _start

    for rpc in made:
        rpc.close()


@pytest.fixture
def wedged() -> Iterator[WedgedHandler]:
    """A handler that never answers until the test is over."""
    handler = WedgedHandler()
    yield handler
    handler.release.set()


@dataclass
class BackgroundCall:
    """A client call made from another thread, so a test can watch it not finish."""

    thread: threading.Thread
    done: threading.Event
    failures: list[RpcError]

    def finished_within(self, seconds: float) -> bool:
        return self.done.wait(timeout=seconds)


def call_in_background(client: RpcClient, method: str) -> BackgroundCall:
    failures: list[RpcError] = []
    done = threading.Event()

    def run() -> None:
        try:
            client.call(method)
        except RpcError as exc:
            # Recorded rather than swallowed: the teardown that releases the
            # handler can make an in-flight call fail, and a test that asserted
            # on an exception nobody kept would be asserting on nothing.
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, name="test-rpc-call", daemon=True)
    thread.start()
    return BackgroundCall(thread=thread, done=done, failures=failures)


posix_only = pytest.mark.skipif(
    local_family() != FAMILY_UNIX,
    reason="a socket file is a filesystem object; a Windows named pipe is not",
)


# ---------------------------------------------------------------------------
# T004 — start brings the server up, writes the descriptor, issues a token
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_publishes_a_descriptor_and_a_token_that_together_connect(
        self, start: Start
    ) -> None:
        started = start(echo)

        assert started.endpoint.descriptor_file.is_file()
        assert started.endpoint.token_file.is_file()
        assert started.endpoint.descriptor_file.name == DESCRIPTOR_FILENAME
        assert started.endpoint.token_file.name == TOKEN_FILENAME
        assert len(read_token(started.state_dir / RUNTIME_DIRNAME)) == MIN_TOKEN_BYTES

        response = started.client().call("session.status", {"n": 7})

        assert response.ok is True
        assert response.result == {"method": "session.status", "n": 7}

    def test_the_published_descriptor_names_this_process_and_a_family_this_build_speaks(
        self, start: Start
    ) -> None:
        started = start(echo)

        published = load_descriptor(started.state_dir)

        assert published.pid == os.getpid()
        assert published.family in {FAMILY_PIPE, FAMILY_UNIX}
        assert published.family == local_family()
        assert published.address == started.endpoint.descriptor.address

    def test_the_descriptor_lives_where_a_second_process_will_look_for_it(
        self, start: Start
    ) -> None:
        """The MCP executable is given a state directory and derives the rest."""
        started = start(echo)

        assert started.endpoint.descriptor_file == (
            started.state_dir / RUNTIME_DIRNAME / DESCRIPTOR_FILENAME
        )
        assert started.endpoint.token_file == started.state_dir / RUNTIME_DIRNAME / TOKEN_FILENAME

    def test_every_run_issues_a_new_token(self, start: Start, tmp_path: Path) -> None:
        """A token that outlived its run would let a stale client into the next one."""
        state_dir = tmp_path / "state"
        first = start(echo, state_dir=state_dir)
        first_key = read_token(state_dir / RUNTIME_DIRNAME)
        first.rpc.close()

        start(echo, state_dir=state_dir)
        second_key = read_token(state_dir / RUNTIME_DIRNAME)

        assert len(first_key) == MIN_TOKEN_BYTES
        assert second_key != first_key

    def test_a_second_endpoint_does_not_steal_a_live_one(
        self, start: Start, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        start(echo, state_dir=state_dir)
        intruder = SidecarRpc(state_dir=state_dir, handler=echo, join_timeout_s=TEST_JOIN_S)

        with pytest.raises(SupervisorError) as caught:
            intruder.start()

        assert str(os.getpid()) in str(caught.value)
        assert intruder.serving is False

    def test_starting_the_same_endpoint_twice_is_refused(self, start: Start) -> None:
        started = start(echo)

        with pytest.raises(SupervisorError):
            started.rpc.start()

        assert started.rpc.serving is True

    @posix_only
    def test_a_socket_file_a_killed_sidecar_left_does_not_block_the_next_start(
        self, start: Start, tmp_path: Path
    ) -> None:
        """`bind` refuses an address that exists, with an error about a filename.

        A sidecar that was killed leaves the socket file and no descriptor. That
        is the state a user restarts from after a crash, and if the next start
        cannot bind, the crash is permanent until somebody deletes a file they
        have never heard of.
        """
        state_dir = tmp_path / "state"
        runtime = state_dir / RUNTIME_DIRNAME
        runtime.mkdir(parents=True)
        leftover = Path(new_address(runtime, family=FAMILY_UNIX))
        leftover.write_bytes(b"")
        assert leftover.exists()

        started = start(echo, state_dir=state_dir)

        assert started.client().call("session.status").ok is True

    @posix_only
    def test_a_start_that_cannot_bind_leaves_no_token_behind(self, tmp_path: Path) -> None:
        """A key on disk for an address that never bound is a secret kept for nothing."""
        state_dir = tmp_path / "state"
        runtime = state_dir / RUNTIME_DIRNAME
        runtime.mkdir(parents=True)
        # A directory where the socket goes: it cannot be unlinked and cannot be
        # bound, so the listener fails after the token has already been issued.
        Path(new_address(runtime, family=FAMILY_UNIX)).mkdir()
        rpc = SidecarRpc(state_dir=state_dir, handler=echo, join_timeout_s=TEST_JOIN_S)

        with pytest.raises(OSError):
            rpc.start()

        assert not (runtime / TOKEN_FILENAME).exists(), "the token outlived the failed bind"
        assert not (runtime / DESCRIPTOR_FILENAME).exists()
        assert rpc.serving is False


# ---------------------------------------------------------------------------
# T005 — after the stop, neither file exists
# ---------------------------------------------------------------------------


class TestStop:
    def test_a_stop_request_ends_with_neither_file_on_disk(self, tmp_path: Path) -> None:
        """The whole of ``pz-agent stop``: a second terminal asks, the sidecar acts.

        Signals are switched off on the asking side because the recorded pid in
        this test is the test runner's own, and ``request_stop`` would otherwise
        deliver SIGTERM to pytest. The control file is the mechanism on every
        platform anyway; the signal only shortens the wait.
        """
        state_dir = tmp_path / "state"
        clock = FakeClock()
        sidecar = SidecarSupervisor(state_dir, clock=clock, signals_supported=False)
        sidecar.pid_file.claim(os.getpid())
        endpoint = sidecar.serve_rpc(echo)
        assert load_descriptor(state_dir).pid == os.getpid()

        terminal = SidecarSupervisor(state_dir, clock=clock, signals_supported=False)
        outcome = terminal.request_stop()

        request = sidecar.control.read()
        assert outcome.requested is True
        assert request is not None and request.kind is ControlKind.STOP

        # What the sidecar's own shutdown path does when it reads that request.
        shutdown = sidecar.stop_rpc()

        assert shutdown.descriptor_removed is True
        assert shutdown.token_revoked is True
        assert shutdown.serving_ended is True
        assert not endpoint.descriptor_file.exists(), "the descriptor outlived the stop"
        assert not endpoint.token_file.exists(), "the token outlived the stop"

    def test_after_the_stop_the_address_it_published_answers_nobody(self, start: Start) -> None:
        """Removing the files while the socket still answers is half a shutdown."""
        started = start(echo)
        descriptor: RpcDescriptor = load_descriptor(started.state_dir)
        key = read_token(started.state_dir / RUNTIME_DIRNAME)
        assert started.client().call("session.status").ok is True

        started.rpc.close()

        with pytest.raises(StaleDescriptor):
            load_descriptor(started.state_dir)
        with pytest.raises(RpcUnavailable):
            RpcClient(descriptor, authkey=key, deadline=TEST_JOIN_S).call("session.status")

    def test_stopping_a_sidecar_that_was_killed_removes_what_it_left_behind(
        self, start: Start, tmp_path: Path
    ) -> None:
        """Nobody else ever will: the process that would have is not ticking."""
        state_dir = tmp_path / "state"
        clock = FakeClock()
        started = start(echo, state_dir=state_dir)
        supervisor = SidecarSupervisor(state_dir, clock=clock, signals_supported=False)
        supervisor.pid_file.claim(os.getpid())
        clock.advance(DEFAULT_STALE_AFTER_MS + 1)

        outcome = supervisor.request_stop()

        assert outcome.requested is True
        assert "not ticking" in outcome.detail
        assert not started.endpoint.descriptor_file.exists()
        assert not started.endpoint.token_file.exists()

    def test_closing_twice_removes_nothing_the_second_time_and_raises_nothing(
        self, start: Start
    ) -> None:
        """``close`` runs from a ``finally`` that a failing shutdown can reach twice."""
        started = start(echo)

        first = started.rpc.close()
        second = started.rpc.close()

        assert (first.descriptor_removed, first.token_revoked) == (True, True)
        assert (second.descriptor_removed, second.token_revoked) == (False, False)
        assert started.rpc.serving is False

    def test_a_supervisor_that_never_served_still_clears_a_previous_run(
        self, start: Start, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        started = start(echo, state_dir=state_dir)

        shutdown = SidecarSupervisor(state_dir, clock=FakeClock()).stop_rpc()

        assert shutdown.descriptor_removed is True
        assert shutdown.token_revoked is True
        assert not started.endpoint.token_file.exists()

    def test_unpublishing_a_state_directory_with_nothing_in_it_says_so(
        self, tmp_path: Path
    ) -> None:
        shutdown = unpublish_rpc(tmp_path / "state")

        assert shutdown.descriptor_removed is False
        assert shutdown.token_revoked is False


# ---------------------------------------------------------------------------
# T006 — the panic stop does not wait on the RPC server
# ---------------------------------------------------------------------------


class TestThePanicStopIsNotOnTheRpcPath:
    def test_the_shipped_join_bound_is_longer_than_the_one_these_tests_use(self) -> None:
        """Otherwise the bound being asserted below is the test's, not the product's."""
        assert TEST_JOIN_S < RPC_JOIN_TIMEOUT_S

    def test_a_panic_stop_completes_while_the_rpc_server_is_blocked(
        self, start: Start, wedged: WedgedHandler, tmp_path: Path
    ) -> None:
        started = start(wedged)
        held = call_in_background(started.client(), "session.status")
        assert wedged.entered.wait(timeout=SETTLE_S), "the handler was never reached"

        # The wedge, proved rather than assumed. The call inside the handler has
        # not returned, and a second call made after it cannot even be accepted:
        # the server answers one connection at a time and its only thread is in
        # the handler. This is a server that genuinely does not answer.
        second = call_in_background(started.client(), "session.status")
        assert not held.finished_within(WEDGE_WATCH_S)
        assert not second.finished_within(WEDGE_WATCH_S)

        layout = IpcLayout(tmp_path / "pz_agent")
        layout.ensure()
        clock = FakeClock()
        port = ExchangeSessionPort(
            layout=layout,
            supervisor=SidecarSupervisor(started.state_dir, clock=clock, signals_supported=False),
            clock=clock,
        )

        began = time.monotonic()
        report = port.stop()
        elapsed = time.monotonic() - began

        assert elapsed < PANIC_BOUND_S, (
            f"the panic stop took {elapsed:.2f}s with the RPC server wedged; "
            "something on that path is waiting for the server"
        )
        assert layout.panic_stop.read_text(encoding="utf-8").strip(), "the latch is empty"
        assert report.disarmed is True
        # And the server really was still wedged for the whole of it, so the
        # bound above was measured against a blocked server and not a recovered one.
        assert started.rpc.serving is True
        assert not held.done.is_set()

    def test_the_shutdown_is_bounded_when_a_handler_never_returns(
        self, start: Start, wedged: WedgedHandler
    ) -> None:
        """A stop that waited for a wedged handler would be a sidecar that cannot stop."""
        started = start(wedged)
        held = call_in_background(started.client(), "session.status")
        assert wedged.entered.wait(timeout=SETTLE_S), "the handler was never reached"

        finished = threading.Event()

        def close_it() -> None:
            started.rpc.close()
            finished.set()

        # On its own thread so an unbounded join fails this test rather than
        # hanging the suite: the assertion below is the bound.
        threading.Thread(target=close_it, name="test-rpc-close", daemon=True).start()

        assert finished.wait(timeout=TEST_JOIN_S + PANIC_BOUND_S), (
            "close did not return; the join is waiting for a handler that never comes back"
        )
        assert not held.done.is_set(), "the handler unwedged itself; this proved nothing"
        assert not started.endpoint.descriptor_file.exists()
        assert not started.endpoint.token_file.exists()

    def test_the_shutdown_reports_the_thread_it_could_not_stop(
        self, start: Start, wedged: WedgedHandler
    ) -> None:
        """Reporting a stopped server whose thread is still in a handler is a fabricated success."""
        started = start(wedged)
        call_in_background(started.client(), "session.status")
        assert wedged.entered.wait(timeout=SETTLE_S), "the handler was never reached"

        shutdown = started.rpc.close()

        assert shutdown.serving_ended is False, "the serving thread is still inside the handler"
        assert shutdown.descriptor_removed is True
        assert shutdown.token_revoked is True

    def test_a_shutdown_with_an_idle_server_does_report_the_thread_ended(
        self, start: Start
    ) -> None:
        """The other half of the previous claim, so `serving_ended` is not always False."""
        started = start(echo)
        assert started.client().call("session.status").ok is True

        shutdown = started.rpc.close()

        assert shutdown.serving_ended is True
        assert started.rpc.serving is False
