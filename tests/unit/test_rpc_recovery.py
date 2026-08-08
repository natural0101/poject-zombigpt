"""What a client and a sidecar do *after* something has already gone wrong.

:mod:`tests.unit.test_rpc_transport` proves the link works and that each wait
on a live peer is bounded; this file proves the recovery paths — the sequences
a user actually walks through when a sidecar crashes, restarts, or leaves its
files behind. Each test is a whole story rather than a single seam, because
the failure it guards against is a *composition*: every step covered, the
sequence still hanging or lying.

The stories, one class each:

* the sidecar dies while a client is waiting on its answer — the client must
  come back with :class:`RpcUnavailable` inside its own deadline, not hang;
* the sidecar restarts on the same runtime directory — the recovery is
  re-reading the descriptor and the token, never retrying the old secret,
  and the refusal of the stale key says exactly that;
* the descriptor's withdrawal is the CLI layer's job, by design — the
  transport's ``close`` leaves the file, and this split is pinned rather than
  papered over, together with the public helper that actually withdraws it;
* a peer that dies between the length prefix and the payload — the serving
  thread absorbs the half-frame and the next client is served;
* a descriptor naming a dead process and an absent socket — the client's
  error names the sidecar as not answering, within the deadline.

Real servers and real sockets throughout, for the same reason the transport
tests refuse doubles: every claim here is about what the operating system does
to a connection whose other end went away.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Client
from pathlib import Path

import pytest

from pz_agent_cli.supervisor import unpublish_rpc
from pz_agent_core.rpc.descriptor import (
    FAMILY_PIPE,
    RpcDescriptor,
    StaleDescriptor,
    load_descriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import issue_token, read_token
from pz_agent_core.rpc.transport import (
    RpcClient,
    RpcServer,
    RpcUnavailable,
    local_family,
    new_address,
)
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse

#: Long enough that a loaded CI runner does not fail the happy path, short
#: enough that a genuine hang ends the test rather than the suite's patience.
GRACE: float = 10.0

#: The deadline a test hands a client whose call is *expected* to fail: short
#: enough to keep the test brisk, long enough that a healthy exchange on a
#: loaded runner would have finished well inside it.
SHORT_DEADLINE: float = 2.0


def _echo(request: RpcRequest) -> RpcResponse:
    return RpcResponse(id=request.id, ok=True, result={"method": request.method, **request.params})


@contextmanager
def _serving(server: RpcServer) -> Iterator[threading.Thread]:
    """Run *server* on its own thread and always end it, bounded, at the exit.

    ``close`` is idempotent, so a test that already killed the server mid-story
    — which is the point of half of this file — pays nothing for the repeat.
    """
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        server.close()
        thread.join(timeout=GRACE)
        assert not thread.is_alive(), "serve_forever did not return after close"


class TestASidecarThatDiesMidCall:
    """The crash window no other test covers: after the request, before the answer.

    ``test_a_client_calling_a_stopped_server_is_told_so`` in the transport
    tests closes the server *between* calls; here the server is closed while a
    client is parked waiting on a handler that will never answer it. The
    client's whole recovery is its deadline — the sidecar is gone and nothing
    will ever push bytes at it — so the claim is that the deadline actually
    fires and surfaces as the transport's own error, not as a hang the MCP
    client above renders as an eternal spinner.
    """

    def test_a_server_closed_under_a_waiting_client_is_reported_within_the_deadline(
        self, tmp_path: Path
    ) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        entered = threading.Event()
        release = threading.Event()

        def stall(request: RpcRequest) -> RpcResponse:
            entered.set()
            # Bounded even so: the test releases it on every exit path, and
            # this ceiling means a broken test cannot park the thread for the
            # whole session.
            release.wait(GRACE * 3)
            return RpcResponse(id=request.id, ok=True)

        server = RpcServer(new_address(runtime), authkey=key, handler=stall)

        def kill_once_the_call_is_inside() -> None:
            if entered.wait(GRACE):
                server.close()

        killer = threading.Thread(target=kill_once_the_call_is_inside, daemon=True)
        killer.start()
        client = RpcClient(server.descriptor(), authkey=key, deadline=SHORT_DEADLINE)
        started = time.monotonic()
        with _serving(server):
            try:
                with pytest.raises(RpcUnavailable):
                    client.call("session.status")
                elapsed = time.monotonic() - started
            finally:
                release.set()
                killer.join(timeout=GRACE)

        assert entered.is_set(), "the call never reached the handler, so nothing was mid-call"
        assert elapsed < GRACE, f"the client outlived its deadline; waited {elapsed:.1f}s"


class TestReconnectAfterARestart:
    """A restart on the same runtime directory issues a new token, on purpose.

    The token module's rule is that a run authorises itself and never whatever
    the last run left behind, so a client that survived the restart holds a
    secret the new server has never seen. Recovery is re-reading the
    descriptor and the token off disk — the two files the new server just
    republished — and the stale key's refusal must say exactly that, because
    retrying the old secret can only ever fail again.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="a named pipe address changes on restart; only the socket path survives one",
    )
    def test_a_stale_key_is_refused_and_a_fresh_read_of_the_files_succeeds(
        self, tmp_path: Path
    ) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        first_key = issue_token(runtime)
        first = RpcServer(new_address(runtime), authkey=first_key, handler=_echo)
        stale = RpcClient(first.descriptor(), authkey=first_key, deadline=GRACE)
        with _serving(first):
            assert stale.call("before.the.restart").ok

        # The restart: same runtime directory, new server, new token issue.
        second_key = issue_token(runtime)
        assert second_key != first_key, "a restart that reuses the key authorises stale clients"
        second = RpcServer(new_address(runtime), authkey=second_key, handler=_echo)
        assert second.address == first.address, (
            "the socket path moved, so the stale client would miss rather than be refused"
        )
        with _serving(second):
            with pytest.raises(RpcUnavailable, match="sidecar restarted"):
                stale.call("with.the.old.key")
            # Retrying the old secret is not the recovery; the same refusal
            # comes back however often it is asked.
            with pytest.raises(RpcUnavailable, match="sidecar restarted"):
                stale.call("with.the.old.key.again")

            fresh = RpcClient(second.descriptor(), authkey=read_token(runtime), deadline=GRACE)
            assert fresh.call("after.the.restart").ok, (
                "re-reading the published files did not reconnect"
            )


class TestWithdrawingTheDescriptorIsTheCliLayers:
    """The documented split: the transport never touches the descriptor file.

    :class:`~pz_agent_core.rpc.transport.RpcServer` binds and unbinds an
    address; the descriptor that *advertises* the address is written by
    ``SidecarRpc.start`` and withdrawn by its ``close`` /
    :func:`~pz_agent_cli.supervisor.unpublish_rpc` — ``core_services`` says in
    so many words that the descriptor and token lifecycle stays the
    supervisor's. This test pins both halves of that split, because the
    dangerous state is the gap between them: a transport-only shutdown leaves
    a descriptor that still *passes* every liveness check — the pid it names
    is this live process, the token file is still there — and a client would
    trust it into dialling a socket nobody serves. The withdrawal, via the
    public helper, is what turns that lie into "the sidecar is not running".
    """

    def test_the_transport_close_leaves_the_descriptor_and_unpublish_withdraws_it(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        runtime = runtime_dir(state)
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        descriptor_file = write_descriptor(state, server.descriptor())

        server.close()

        assert descriptor_file.exists(), (
            "RpcServer.close removed the descriptor; the documented split moved "
            "and the supervisor's withdrawal is now dead code"
        )
        survived = load_descriptor(state)
        assert survived.address == server.address, (
            "what survived the transport shutdown is not even the published address"
        )

        shutdown = unpublish_rpc(state)

        assert shutdown.descriptor_removed is True
        assert shutdown.token_revoked is True
        with pytest.raises(StaleDescriptor, match="no descriptor"):
            load_descriptor(state)


class TestAPartialFrame:
    """A peer that dies between the length prefix and the payload it promised.

    The transport tests cover a peer that *holds* mid-frame (the trickle, cut
    at the idle budget) and a peer that closes before sending anything. The
    remaining shape is the crash mid-write: four header bytes on the wire, the
    payload never coming, the connection already gone. The server's read ends
    in an immediate end-of-file rather than a wait, and the claim that matters
    is that the one serving thread absorbs it and answers the next caller.
    """

    def test_a_length_prefix_with_no_payload_does_not_take_the_serving_thread_down(
        self, tmp_path: Path
    ) -> None:
        if sys.platform == "win32":
            pytest.skip("the half-frame is written to a raw socket file descriptor")
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=_echo)
        with _serving(server) as thread:
            connection = Client(server.address, family="AF_UNIX", authkey=key)
            os.write(connection.fileno(), struct.pack("!i", 64))
            connection.close()

            after = RpcClient(server.descriptor(), authkey=key, deadline=GRACE)
            assert after.call("after.the.half.frame").ok, (
                "the half-frame took the serving thread down with it"
            )
            assert thread.is_alive()


class TestADeadPidDescriptor:
    """A descriptor for a sidecar that is dead and cleaned its socket away.

    The descriptor layer's own liveness check is covered elsewhere
    (``test_rpc_token_and_descriptor``); this is the layer below it — a client
    that was handed the descriptor *before* the sidecar died, so no on-disk
    check ever ran for it. Its dial finds nothing at the address, and the
    error must name the sidecar as not answering: that is the message that
    sends a user to the launcher, where "connection refused" out of the socket
    layer sends them nowhere.
    """

    def test_a_descriptor_naming_a_dead_process_reads_as_the_sidecar_not_answering(
        self, tmp_path: Path
    ) -> None:
        with subprocess.Popen([sys.executable, "-c", ""]) as child:
            child.wait(timeout=GRACE)
        # `wait` returned, so `child.pid` names a process that has exited; the
        # address below was never bound, matching a sidecar that cleaned its
        # socket away (or a pipe, which vanishes with its process) as it died.
        address = (
            r"\\.\pipe\pz-agent-recovery-nobody-here"
            if local_family() == FAMILY_PIPE
            else str(tmp_path / "runtime" / "absent.sock")
        )
        descriptor = RpcDescriptor(address=address, family=local_family(), pid=child.pid)
        client = RpcClient(descriptor, authkey=b"k" * 32, deadline=SHORT_DEADLINE)

        started = time.monotonic()
        with pytest.raises(RpcUnavailable, match="not answering"):
            client.call("session.status")

        assert time.monotonic() - started < GRACE
