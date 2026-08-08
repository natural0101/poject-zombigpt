"""The chain under attack, spoken to over the same pipes a client owns.

``test_mcp_subprocess_e2e`` proves the happy chain: a real ``pz-agent-mcp``
child, a real sidecar, a real answer. This file breaks that chain on purpose,
one link at a time, *while the child is serving* — because every failure here
is one a user's machine actually produces mid-session (a sidecar restart
rewrites the token; a kill leaves the descriptor; a wedged core answers
nothing) and the promise under test is always the same double one: the failing
call comes back as the boundary's error document, and the process survives to
answer a healthy call afterwards. A child that died, hung, or printed a
traceback onto its protocol stream fails the second half, and only a subprocess
test can see it.

The client here is deliberately not the SDK's. It is a hand-rolled speaker of
newline-delimited JSON-RPC over the child's pipes, for the one thing the SDK
client cannot give: both raw streams, byte for byte. Half the assertions in
this file are about what must *not* be on them — token bytes, state paths,
tracebacks, any line that is not protocol — and a client that parses the stream
for you has already thrown away the evidence.
"""

from __future__ import annotations

import json
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, Final, cast

import pytest

from pz_agent_core.protocol import JsonDict, Observation
from pz_agent_core.rpc.descriptor import descriptor_path, runtime_dir, write_descriptor
from pz_agent_core.rpc.token import TOKEN_FILENAME, issue_token
from pz_agent_core.rpc.transport import DEFAULT_DEADLINE_SECONDS, RpcServer, new_address
from pz_agent_mcp.remote.server import CoreRouter
from tests.contract.test_mcp_subprocess_e2e import (
    _ALL_CAPABILITIES,
    _COMPLAINT_CAP,
    _EAT_REF,
    _HAS_SDK,
    _PUBLISHED_RESOURCES,
    GRACE,
    Sidecar,
    _child_env,
)
from tests.fixtures.mcp_doubles import Doubles, FakeObservationPort, make_report

#: Every test in this file drives the *serving* child — there is no refusal-only
#: case here that could run without the SDK — so the skip is honest at module
#: scope: on an install without the extra, nothing in this file is testable.
pytestmark = [pytest.mark.contract, _HAS_SDK]

#: The protocol date offered in ``initialize``. The server negotiates its own
#: version back and nothing here pins which; the offer only has to be a date the
#: SDK will answer rather than refuse.
_PROTOCOL_DATE: Final = "2025-06-18"

#: How much of the child's stderr is read after it exits. One connection line is
#: expected; anything more is what the assertions are for — but the read stays
#: bounded, because "bounded everything" includes reads of a stream an assertion
#: is about to fail on.
_STDERR_CAP: Final = _COMPLAINT_CAP * 8


def _start_sidecar(tmp_path: Path, core: Doubles) -> Sidecar:
    """A running core for *core*, wired exactly as the happy-path fixture wires it.

    A local copy of the ``sidecar`` fixture's body rather than the fixture,
    because half the tests here need to choose the ``Doubles`` before the server
    wraps them — `CoreRouter` captures the port objects at construction, so a
    stalling port swapped in afterwards would never be reached.
    """
    state_dir = tmp_path / "pz-agent"
    runtime_dir(state_dir).mkdir(parents=True)
    key = issue_token(runtime_dir(state_dir))
    server = RpcServer(
        new_address(runtime_dir(state_dir)),
        authkey=key,
        handler=CoreRouter(core.services, goals=core.goals),
    )
    write_descriptor(state_dir, server.descriptor())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return Sidecar(state_dir=state_dir, core=core, server=server, thread=thread)


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[Sidecar]:
    running = _start_sidecar(tmp_path, Doubles())
    try:
        yield running
    finally:
        running.stop()


class _StallingObservationPort(FakeObservationPort):
    """An observation port that answers nothing until the test lets it.

    The shape of a wedged core: the process is alive, the listener accepts, the
    handshake completes, and the handler never returns. Waiting on an
    :class:`~threading.Event` rather than sleeping makes the stall exactly as
    long as the test needs and not a second longer — and the wait itself is
    bounded by ``GRACE``, because a test that failed before releasing it must
    not leave a server thread parked past the suite's patience.
    """

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def latest(self) -> Observation | None:
        # The bounded fallthrough: if nothing ever releases the stall, answer
        # after GRACE anyway. By then the client's own deadline has long fired,
        # so the answer lands on a severed connection and changes nothing.
        self.release.wait(timeout=GRACE)
        return super().latest()


class RawClient:
    """Launches ``pz-agent-mcp`` and speaks newline-delimited JSON-RPC to it.

    Everything the child writes to stdout is kept raw and checked to be a JSON
    line; stderr is collected after exit. Every read is bounded — a queue with a
    deadline in front of a reader thread — so a child that stops answering
    fails a test instead of hanging the suite.
    """

    def __init__(self, state_dir: Path) -> None:
        self._child: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-m", "pz_agent_mcp", "--state-dir", str(state_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(),
        )
        assert self._child.stdin is not None
        assert self._child.stdout is not None
        assert self._child.stderr is not None
        self._stdin: IO[bytes] = self._child.stdin
        self._stdout: IO[bytes] = self._child.stdout
        self._stderr: IO[bytes] = self._child.stderr
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0
        self._code: int | None = None
        self.stdout_raw = bytearray()
        self.stderr_raw = b""
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def __enter__(self) -> RawClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()

    def _pump(self) -> None:
        for line in self._stdout:
            self.stdout_raw += line
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, payload: JsonDict) -> None:
        self._stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self._stdin.flush()

    @staticmethod
    def _decoded(line: bytes) -> JsonDict:
        """One stdout line, which the protocol says must be one JSON object."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"a non-protocol line reached stdout: {line[:200]!r}")
        assert isinstance(message, dict), f"a non-object frame reached stdout: {line[:200]!r}"
        return cast(JsonDict, message)

    def request(self, method: str, params: JsonDict, *, deadline: float = GRACE) -> JsonDict:
        """Send one request and wait — boundedly — for the answer bearing its id."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        until = time.monotonic() + deadline
        while True:
            remaining = until - time.monotonic()
            try:
                line = self._lines.get(timeout=max(remaining, 0.01))
            except queue.Empty:
                pytest.fail(f"no answer to {method} within {deadline:g}s")
            assert line is not None, f"stdout closed before the answer to {method}"
            message = self._decoded(line)
            # Notifications and other ids are somebody else's frames; skipping
            # them is what lets this loop wait for exactly one answer.
            if message.get("id") == rid:
                return message

    def hello(self) -> JsonDict:
        """The handshake, returning the ``initialize`` result (``serverInfo`` et al)."""
        answer = self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_DATE,
                "capabilities": {},
                "clientInfo": {"name": "adversary", "version": "0"},
            },
        )
        assert "error" not in answer, f"initialize was refused: {answer!r}"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = answer["result"]
        assert isinstance(result, dict), f"initialize answered no object: {answer!r}"
        return cast(JsonDict, result)

    def call(self, name: str, arguments: JsonDict, *, deadline: float = GRACE) -> JsonDict:
        """One tool call, unwrapped to the boundary's own payload document.

        A refusal must arrive *as a payload* — reason code, retry flag — and
        never as a JSON-RPC error, which carries neither; the unwrap asserts
        that shape so every test in this file states it for free.
        """
        answer = self.request(
            "tools/call", {"name": name, "arguments": arguments}, deadline=deadline
        )
        assert "error" not in answer, f"{name} answered a transport error: {answer!r}"
        content = answer["result"]["content"]
        assert len(content) == 1, f"{name} answered {len(content)} content blocks"
        assert content[0]["type"] == "text"
        document = json.loads(content[0]["text"])
        assert isinstance(document, dict), f"{name} answered no object"
        return cast(JsonDict, document)

    def shutdown(self) -> int:
        """Close stdin — the clean goodbye — and collect the exit code and stderr.

        Idempotent, and bounded at every step: a child that ignores EOF is
        killed rather than waited on, because a hang here is a finding about
        the server, not a reason to hold the suite.
        """
        if self._code is None:
            if not self._stdin.closed:
                self._stdin.close()
            try:
                self._code = self._child.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
                self._child.kill()
                self._code = self._child.wait(timeout=GRACE)
            self._reader.join(timeout=GRACE)
            self.stderr_raw = self._stderr.read(_STDERR_CAP)
            self._stderr.close()
            self._stdout.close()
        return self._code

    @property
    def stderr_text(self) -> str:
        return self.stderr_raw.decode("utf-8", errors="replace")

    def assert_protocol_clean(self) -> None:
        """Every byte that crossed stdout parsed as a JSON frame.

        Callable only after :meth:`shutdown`, when the reader thread has
        drained the pipe; the check is over the whole session's stream, so a
        stray line written during a failure between two healthy answers cannot
        hide.
        """
        assert self._code is not None, "assert_protocol_clean before shutdown reads a live buffer"
        for line in bytes(self.stdout_raw).splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"a non-protocol line reached stdout: {line[:200]!r}")


class TestATamperedTokenIsRefusedWithoutLeaking:
    """A token of the right length and the wrong bytes, swapped in mid-session.

    That is what a sidecar restart looks like from the child's side — the file
    is there, the length is right, the secret is not the one the server holds.
    The handshake must fail *as authentication*, arrive as the link-down error
    document, leave the process serving, and put not one byte of either secret
    on either stream. The token file is resolved per call, which is also why
    restoring it heals the same connection without a restart.
    """

    def test_the_link_refuses_and_the_child_survives(self, sidecar: Sidecar) -> None:
        token_path = runtime_dir(sidecar.state_dir) / TOKEN_FILENAME
        original = token_path.read_bytes()
        tampered = secrets.token_bytes(len(original))
        assert tampered != original

        with RawClient(sidecar.state_dir) as client:
            client.hello()
            control = client.call("pz_session_status", {})
            assert control["ok"] is True, "the positive control failed; the link never worked"

            token_path.write_bytes(tampered)
            refused = client.call("pz_session_status", {})

            token_path.write_bytes(original)
            healed = client.call("pz_session_status", {})
            code = client.shutdown()

        assert refused["ok"] is False, refused
        assert refused["reason_code"] == "STALE_SESSION"
        assert refused["retryable"] is False
        assert "the token does not match" in refused["message"]

        assert healed["ok"] is True, "the child did not survive to answer a healthy call"
        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"

        both_streams = bytes(client.stdout_raw) + client.stderr_raw
        for secret in (original, tampered):
            assert secret not in both_streams, "raw token bytes reached a stream"
            assert secret.hex().encode("ascii") not in both_streams, (
                "a hex spelling of the token reached a stream"
            )


class TestADeletedDescriptorMidSessionIsLinkDown:
    """The descriptor vanishes under an established MCP connection.

    A sidecar shutting down deletes exactly this file, so the next call finds
    the address gone. The answer has to be the boundary's error document naming
    the sidecar as not running — with the remedy — not a crash and not a hang,
    and a descriptor written back must make the very same child reachable
    again, because nothing about the link is cached.
    """

    def test_the_next_call_answers_link_down_and_the_child_survives(self, sidecar: Sidecar) -> None:
        with RawClient(sidecar.state_dir) as client:
            client.hello()
            control = client.call("pz_session_status", {})
            assert control["ok"] is True, "the positive control failed; the link never worked"

            descriptor_path(sidecar.state_dir).unlink()
            down = client.call("pz_session_status", {})

            # The sidecar itself never stopped; only its address was lost. A
            # rewritten descriptor is what its own supervisor would produce.
            write_descriptor(sidecar.state_dir, sidecar.server.descriptor())
            healed = client.call("pz_session_status", {})
            code = client.shutdown()

        assert down["ok"] is False, down
        assert down["reason_code"] == "STALE_SESSION"
        assert down["retryable"] is False, "no number of retries writes a descriptor"
        assert "the sidecar is not running or has stopped answering" in down["message"]
        assert "pz-agent start" in down["message"]

        assert healed["ok"] is True, "the child did not survive to answer a healthy call"
        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"


class TestASidecarKilledBetweenTwoCalls:
    """The core dies between two tool calls on one connection.

    The second call must error within a bound — a dead socket answers at once,
    and nothing may turn that into a wait — while the stream that carried
    ``serverInfo`` stays protocol-clean to the last byte and the child goes on
    serving what needs no link. This overlaps the happy-path file's lost-sidecar
    test on purpose; what is new here is the raw stream and the clock.
    """

    def test_the_second_call_errors_bounded_and_the_stream_stays_clean(
        self, sidecar: Sidecar
    ) -> None:
        with RawClient(sidecar.state_dir) as client:
            initialised = client.hello()
            assert initialised["serverInfo"]["name"] == "pz-agent"
            control = client.call("pz_session_status", {})
            assert control["ok"] is True, "the positive control failed; the link never worked"

            sidecar.stop()
            started = time.monotonic()
            lost = client.call("pz_session_status", {})
            elapsed = time.monotonic() - started

            catalogue = client.request("resources/list", {})
            code = client.shutdown()

        assert lost["ok"] is False, lost
        assert lost["reason_code"] == "STALE_SESSION"
        assert lost["retryable"] is False
        assert elapsed < GRACE, f"a dead sidecar took {elapsed:.1f}s to be reported"

        served = tuple(
            (entry["uri"], entry["name"], entry["mimeType"])
            for entry in catalogue["result"]["resources"]
        )
        assert served == _PUBLISHED_RESOURCES, "the child stopped serving after the link died"

        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"
        client.assert_protocol_clean()


class TestADuplicateKeyToAMutatingToolReplays:
    """The same idempotency key twice, through the whole chain.

    The recorded semantics: the duplicate's answer equals the original — same
    action id, same document, same status — marked ``replayed``, and the core
    is asked to do the work exactly once. Both halves matter and only one is
    visible from the wire, which is why the doubles live in this process: an
    answer can be right about work that happened twice.
    """

    def test_the_duplicate_answers_the_original_result(self, sidecar: Sidecar) -> None:
        sidecar.core.capabilities.report_value = make_report(usable=_ALL_CAPABILITIES)
        arguments: JsonDict = {"item_ref": _EAT_REF, "idempotency_key": "adversary:eat:1"}

        with RawClient(sidecar.state_dir) as client:
            client.hello()
            first = client.call("pz_action_eat", dict(arguments))
            second = client.call("pz_action_eat", dict(arguments))
            fresh = client.call(
                "pz_action_eat", {"item_ref": _EAT_REF, "idempotency_key": "adversary:eat:2"}
            )
            code = client.shutdown()

        assert first["ok"] is True, first
        assert first["replayed"] is False
        assert second["ok"] is True, second
        assert second["replayed"] is True
        assert second["action_id"] == first["action_id"]
        assert second["data"] == first["data"]
        assert second["status"] == first["status"]
        # A fresh request id proves the equality above is the recorded call
        # being replayed, not one response frame read twice.
        assert second["request_id"] != first["request_id"]

        # The healthy follow-up is itself the control: a fresh key still
        # reaches the core, so the replay path did not wedge the submit path.
        assert fresh["ok"] is True, fresh
        assert fresh["replayed"] is False
        assert fresh["action_id"] != first["action_id"]

        keys = [request.idempotency_key for request in sidecar.core.actions.submitted]
        assert keys == ["adversary:eat:1", "adversary:eat:2"], (
            "the duplicate reached the action port; a replay that submits again is "
            "not idempotent, whatever it answers"
        )
        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"


class TestInvalidParamsAreATypedRefusal:
    """Arguments the tool's own published schema rejects.

    The answer must be the boundary's error document with ``INVALID_ARGUMENT``
    — the typed refusal a client can branch on — and never a traceback on
    stderr, because a traceback carries frames, and frames carry the state
    directory path, which runs through the user's profile.
    """

    def test_a_schema_violation_is_a_payload_not_a_traceback(self, sidecar: Sidecar) -> None:
        with RawClient(sidecar.state_dir) as client:
            client.hello()
            # Wrong in two schema-checked ways at once: `item_ref` is not a
            # string and the required `idempotency_key` is absent.
            refused = client.call("pz_action_eat", {"item_ref": 42})
            healthy = client.call("pz_session_status", {})
            code = client.shutdown()

        assert refused["ok"] is False, refused
        assert refused["reason_code"] == "INVALID_ARGUMENT"
        assert refused["retryable"] is False, "the same arguments will never become valid"
        assert "idempotency_key" in refused["message"], (
            "the refusal does not name the field a caller has to fix"
        )
        assert sidecar.core.actions.submitted == [], (
            "arguments the schema rejects reached the action port"
        )

        assert healthy["ok"] is True, "the child did not survive to answer a healthy call"
        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"
        assert "Traceback" not in client.stderr_text, client.stderr_text[:500]
        assert str(sidecar.state_dir) not in client.stderr_text, (
            "the state directory path reached stderr; it runs through the profile "
            "and carries the account name"
        )
        client.assert_protocol_clean()


class TestAStalledCoreIsCutAtTheDeadline:
    """A core that answers, then holds the next call open forever.

    The worst link failure, because nothing is visibly broken: the process is
    alive, the descriptor is live, the handshake completes, and the handler
    never returns. The client's whole-call deadline is what turns that into an
    answer, so the stalled tool call must come back as the link-down document
    naming the budget, within it — and once the core is released, the same
    child serves on.
    """

    def test_the_call_surfaces_the_deadline_error_within_budget(self, tmp_path: Path) -> None:
        release = threading.Event()
        stalled_core = Doubles(observations=_StallingObservationPort(release))
        running = _start_sidecar(tmp_path, stalled_core)
        try:
            with RawClient(running.state_dir) as client:
                client.hello()
                # The control avoids the observation port on purpose: the core
                # is healthy on every other route, which is what makes the
                # stall a stall rather than a dead sidecar.
                control = client.call("pz_debug_doctor", {})
                assert control["ok"] is True, "the positive control failed"

                started = time.monotonic()
                stalled = client.call("pz_observe_snapshot", {"detail": "standard"})
                elapsed = time.monotonic() - started

                release.set()
                healthy = client.call("pz_debug_doctor", {})
                code = client.shutdown()
        finally:
            release.set()
            running.stop()

        assert stalled["ok"] is False, stalled
        assert stalled["reason_code"] == "STALE_SESSION"
        assert f"no answer within {DEFAULT_DEADLINE_SECONDS:g}s" in stalled["message"], (
            "the deadline fired but the answer does not name the budget"
        )
        # Both bounds. The lower one proves the budget was really spent — a
        # call refused for some faster reason would name a deadline it never
        # waited for. The upper one is the promise: a client's spinner ends.
        assert elapsed >= DEFAULT_DEADLINE_SECONDS - 0.5, f"refused in {elapsed:.1f}s, not stalled"
        assert elapsed < GRACE, f"the deadline error took {elapsed:.1f}s to surface"

        assert healthy["ok"] is True, "the child did not survive the stalled call"
        assert code == 0, f"the server exited {code}: {client.stderr_text[:500]}"
