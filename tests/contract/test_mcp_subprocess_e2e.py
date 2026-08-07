"""The whole chain, in the shape a client actually meets it.

An MCP client launches ``pz-agent-mcp`` as a subprocess and speaks JSON-RPC over
its stdin and stdout. Nothing this project has tested so far runs that way: the
entry point was driven in-process with a captured stream, the router was driven
against a fake, the link was driven over a socket in one process. Each is right
about its own layer, and none of them can see the two things that only exist
once there is a real child:

**stdout is the protocol.** A single stray byte — a warning, a banner, a log
line, a traceback — is not a cosmetic problem. It is a parse error in the
client, and the client will report it as *our* server being broken. In-process
tests capture a `TextIO` that nobody is parsing, so they cannot notice.

**The exit code is the diagnosis.** A client author whose server refused has
exactly one number to go on. It has to be the number the document promises,
produced by the real executable under the real condition, not by a function
returning an integer.

So every test here launches ``python -m pz_agent_mcp`` for real, with stdin
closed or spoken to, and asserts on what came back down the pipes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

import pz_agent_core
import pz_agent_mcp
import pz_agent_mcp.__main__ as entry
from pz_agent_core.rpc.descriptor import descriptor_path, runtime_dir, write_descriptor
from pz_agent_core.rpc.token import TOKEN_FILENAME, issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_mcp.remote.server import CoreRouter
from tests.fixtures.mcp_doubles import Doubles

pytestmark = pytest.mark.contract

GRACE: Final = 30.0


def _sdk_present() -> bool:
    try:
        import mcp  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


#: The SDK is an optional extra. Where it is absent the stdio server cannot be
#: exercised at all — but `--describe`, `--version` and every refusal that fires
#: before the SDK gate can, and those are the ones a client author meets first.
#: Marked per test rather than per module, so a skip here never silently takes
#: the whole file with it and leaves the file looking green.
_HAS_SDK = pytest.mark.skipif(
    not _sdk_present(),
    reason="the mcp extra is not installed; the stdio server cannot be exercised",
)


def _child_env() -> dict[str, str]:
    """The import path the child needs, derived from where we found the packages.

    Not a hard-coded ``packages/*/src``: that would pass here and tell an
    installed distribution nothing.
    """
    roots = {
        str(Path(module.__file__ or "").resolve().parents[1])
        for module in (pz_agent_mcp, pz_agent_core)
    }
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*sorted(roots), *([existing] if existing else [])])
    return env


def _run(*argv: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pz_agent_mcp", *argv],
        capture_output=True,
        text=True,
        timeout=GRACE,
        input=stdin.decode() if stdin is not None else None,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        env=_child_env(),
        check=False,
    )


@dataclass
class Sidecar:
    """A running core, as the child will find it: a descriptor and a token."""

    state_dir: Path
    core: Doubles
    server: RpcServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[Sidecar]:
    state_dir = tmp_path / "pz-agent"
    runtime_dir(state_dir).mkdir(parents=True)
    key = issue_token(runtime_dir(state_dir))
    core = Doubles()
    server = RpcServer(
        new_address(runtime_dir(state_dir)), authkey=key, handler=CoreRouter(core.services)
    )
    write_descriptor(state_dir, server.descriptor())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    running = Sidecar(state_dir=state_dir, core=core, server=server, thread=thread)
    try:
        yield running
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# what answers with nothing running
# ---------------------------------------------------------------------------


class TestTheSurfaceIsReadableWithNothingRunning:
    """The promise `configs/mcp/README.md` makes to somebody writing a client."""

    def test_describe_writes_the_catalogue_and_exits_zero(self) -> None:
        result = _run("--describe")

        assert result.returncode == entry.EXIT_OK, result.stderr
        document = json.loads(result.stdout)
        assert document["tools"], "the catalogue published no tools"
        assert document["server"] == "pz-agent"

    def test_version_answers(self) -> None:
        result = _run("--version")

        assert result.returncode == entry.EXIT_OK, result.stderr
        assert result.stdout.strip() or result.stderr.strip()

    def test_describe_needs_no_sidecar_and_no_state_directory(self, tmp_path: Path) -> None:
        """Both are promised to be unnecessary; only running it can show that."""
        result = _run("--describe", "--state-dir", str(tmp_path / "nothing-here"))

        assert result.returncode == entry.EXIT_OK, result.stderr
        assert json.loads(result.stdout)["tools"]


class TestARefusalIsDiagnosable:
    def test_no_two_refusals_share_a_code(self) -> None:
        """Every test below compares against `entry.EXIT_*`, which cannot on its
        own notice a collapse.

        Renaming a constant's *value* moves the assertion with it: the test reads
        the same module the program does, so `EXIT_DESCRIPTOR_UNREADABLE = 1`
        satisfies `returncode == entry.EXIT_DESCRIPTOR_UNREADABLE` while making a
        corrupt descriptor indistinguishable from an absent sidecar. Found by
        mutating exactly that and watching nothing fail.

        Distinctness is the property the constants exist for, so it is asserted
        directly rather than inferred from any single comparison.
        """
        codes = {
            name: value
            for name, value in vars(entry).items()
            if name.startswith("EXIT_") and isinstance(value, int)
        }
        shared: dict[int, list[str]] = {}
        for name, value in codes.items():
            shared.setdefault(value, []).append(name)
        collapsed = {value: sorted(names) for value, names in shared.items() if len(names) > 1}

        assert collapsed == {}, (
            f"these refusals cannot be told apart by their exit code: {collapsed}"
        )
        assert len(codes) >= 9, f"only {len(codes)} exit codes found; the set has shrunk"

    def test_naming_both_directories_is_a_usage_error(self, tmp_path: Path) -> None:
        """Two ways of naming one directory. Honouring one silently would
        connect a client to a sidecar it did not ask for."""
        result = _run("--state-dir", str(tmp_path), "--zomboid-dir", str(tmp_path))

        assert result.returncode == entry.EXIT_USAGE
        assert result.stderr.strip(), "a refusal with no explanation is not one"

    def test_no_descriptor_says_the_sidecar_is_not_running(self, tmp_path: Path) -> None:
        empty = tmp_path / "never-started"
        empty.mkdir()

        result = _run("--state-dir", str(empty))

        assert result.returncode in {entry.EXIT_NOT_WIRED, entry.EXIT_STALE_DESCRIPTOR}, (
            f"exit {result.returncode}: {result.stderr}"
        )
        assert result.stderr.strip()

    def test_an_unreadable_descriptor_is_its_own_refusal(self, tmp_path: Path) -> None:
        """Distinct from "not running": restarting will not fix a corrupt file."""
        state_dir = tmp_path / "corrupt"
        runtime_dir(state_dir).mkdir(parents=True)
        issue_token(runtime_dir(state_dir))
        descriptor_path(state_dir).write_text("{ not json", encoding="utf-8")

        result = _run("--state-dir", str(state_dir))

        assert result.returncode == entry.EXIT_DESCRIPTOR_UNREADABLE, result.stderr

    def test_every_refusal_keeps_stdout_empty(self, tmp_path: Path) -> None:
        """The assertion only a subprocess can make.

        A client is parsing stdout as JSON-RPC from the first byte. A refusal
        that printed its reason there would be reported as our server emitting
        malformed protocol, not as the sidecar being absent.
        """
        cases = [
            ("--state-dir", str(tmp_path / "absent")),
            ("--state-dir", str(tmp_path), "--zomboid-dir", str(tmp_path)),
        ]
        for argv in cases:
            result = _run(*argv)

            assert result.stdout == "", f"{argv} wrote to stdout: {result.stdout!r}"
            assert result.stderr.strip(), f"{argv} refused without saying why"


class TestNoSecretReachesEitherStream:
    """The token authenticates the link. It must not be observable from outside."""

    def test_neither_the_token_nor_the_address_is_printed(self, sidecar: Sidecar) -> None:
        token = (runtime_dir(sidecar.state_dir) / TOKEN_FILENAME).read_bytes()

        result = _run("--describe", "--state-dir", str(sidecar.state_dir))
        combined = result.stdout + result.stderr

        assert token.hex() not in combined
        assert token.decode("latin-1") not in combined
        assert sidecar.server.address not in combined

    def test_a_refusal_does_not_print_the_state_directory(self, tmp_path: Path) -> None:
        """The path runs through the user's profile, so it carries their name."""
        profile = tmp_path / "Users" / "Иван" / "Zomboid" / "pz-agent"
        profile.mkdir(parents=True)

        result = _run("--state-dir", str(profile))

        assert "Иван" not in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# the protocol itself
# ---------------------------------------------------------------------------


def _rpc(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class TestTheStdioServerSpeaksTheProtocol:
    @_HAS_SDK
    def test_it_starts_against_a_running_sidecar_and_stops_when_stdin_closes(
        self, sidecar: Sidecar
    ) -> None:
        """The one thing every other test in this file assumes.

        Not asserted about the protocol yet: asserted that the process reaches
        the serving state at all, against a real core, and then ends rather than
        hanging when its client goes away. A server that had to be killed would
        leave a process behind on every client restart.
        """
        # A context manager, not a bare Popen: the pipes are files, and this
        # project turns ResourceWarning into an error — correctly, since a test
        # leaking descriptors is a test that will fail somebody else later.
        with subprocess.Popen(
            [sys.executable, "-m", "pz_agent_mcp", "--state-dir", str(sidecar.state_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(),
            text=True,
        ) as child:
            try:
                assert child.stdin is not None
                child.stdin.close()
                child.wait(timeout=GRACE)
            finally:
                if child.poll() is None:  # pragma: no cover - only on a hang
                    child.kill()
                    child.wait(timeout=GRACE)
            code = child.poll()

        assert code is not None, "the server did not exit when its stdin closed"

    @_HAS_SDK
    def test_nothing_but_protocol_reaches_stdout_while_serving(self, sidecar: Sidecar) -> None:
        """Every byte on stdout has to be something a JSON-RPC reader accepts."""
        with subprocess.Popen(
            [sys.executable, "-m", "pz_agent_mcp", "--state-dir", str(sidecar.state_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(),
        ) as child:
            try:
                # `communicate` closes stdin itself, which is the EOF the server
                # exits on. Closing it first makes `communicate` flush a file it
                # no longer owns.
                stdout, _ = child.communicate(timeout=GRACE)
            finally:
                if child.poll() is None:  # pragma: no cover - only on a hang
                    child.kill()
                    child.wait(timeout=GRACE)

        text = stdout.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if not line.strip():
                continue
            assert line.startswith(("Content-Length:", "{", "[")) or line.startswith("\r"), (
                f"a non-protocol line reached stdout: {line!r}"
            )


class TestTheCatalogueMatchesWhatIsServed:
    def test_describe_and_the_router_publish_the_same_tools(self) -> None:
        """`--describe` is what a client author reads. If it disagrees with what
        the server answers, they write against a surface that does not exist."""
        result = _run("--describe")
        described = {tool["name"] for tool in json.loads(result.stdout)["tools"]}

        from pz_agent_mcp.catalog import TOOLS  # noqa: PLC0415

        assert described == {spec.descriptor()["name"] for spec in TOOLS}

    def test_the_described_surface_is_json_a_client_can_parse(self) -> None:
        """Run rather than imported: the claim is about the executable."""
        result = _run("--describe")

        document = json.loads(result.stdout)

        assert isinstance(document["tools"], list)
        assert isinstance(document["resources"], list)
        for tool in document["tools"]:
            assert tool["inputSchema"]["type"] == "object", tool["name"]
