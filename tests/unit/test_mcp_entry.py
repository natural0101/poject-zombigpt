"""The ``pz-agent-mcp`` entry point, driven the way a client launches it.

Every test here calls :func:`pz_agent_mcp.__main__.main` in this process with an
argv and two captured streams, because that is the whole contract of that
function: it returns an exit code, it never raises for a user error, and the
only bytes it produces go to the two streams it was handed. A subprocess test
could assert the code and the output, but it could not assert what the module
did *not* do — that no directory was read when an embedder supplied its own
ports, that the SDK gate fired before anything went looking for a sidecar — and
those are the properties that decide which remedy a user is sent to.

Three things are checked that a narrower test would miss.

**The refusals are distinguished, not merely produced.** "No sidecar", "the
sidecar died without cleaning up", "there are two installs here" and "that file
is not a descriptor" are four different mornings, and the code is the only thing
a client author can branch on. So each is provoked from a real state directory
on disk with a real descriptor in it, and each is asserted to be *not* the codes
next to it. Asserting only that a bad state directory fails would pass with all
four collapsed into one.

**Nothing reaches stdout unless it is the answer to a question.** Once serving
begins, stdout is a JSON-RPC stream an MCP client is parsing, and one stray line
corrupts it for every client. Every non-``--describe`` invocation in this file
asserts an empty stdout, and the connected case asserts that neither the token
nor the address is on either stream — a support bundle and a screenshot are
exactly where those must not turn up.

**The connected path is connected.** The serving tests run a real
:class:`RpcServer` over a real socket with the real :class:`CoreRouter` in front
of the fake ports, exactly as ``tests/contract/test_remote_core_round_trip.py``
does, and then assert that the services :func:`main` handed to the stdio server
answer with the fake core's own snapshot. A monkeypatched
``RemoteCoreServices`` would have proved that a constructor was called.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import pytest

import pz_agent_cli.context as cli_context
from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    FAMILY_UNIX,
    RpcDescriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import TOKEN_FILENAME, issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address, unix_socket_supported
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from pz_agent_core.version import PRODUCT_VERSION, RPC_PROTOCOL_VERSION, protocol_major
from pz_agent_mcp import __main__ as entry
from pz_agent_mcp.catalog import RESOURCES, TOOLS
from pz_agent_mcp.ports import CoreServices
from pz_agent_mcp.remote.client import RemoteCoreServices
from pz_agent_mcp.remote.server import CoreRouter
from pz_agent_mcp.server import SERVER_NAME, McpSdkUnavailable
from tests.fixtures.mcp_doubles import Doubles

#: Long enough for a loaded runner, short enough that a hang ends the test.
GRACE: Final = 10.0

#: The transport binds a Unix socket here and a named pipe on Windows. Every
#: test that needs a *listening* server is gated on the family this platform
#: actually binds; the descriptor-only tests are not, because a descriptor is a
#: JSON document and its failure modes are the same everywhere.
LISTENS_HERE: Final = sys.platform == "win32" or unix_socket_supported()

needs_a_socket = pytest.mark.skipif(
    not LISTENS_HERE, reason="no transport family binds on this platform"
)


# ---------------------------------------------------------------------------
# driving main()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One invocation of the entry point and everything it emitted."""

    code: int
    out: str
    err: str


def run_main(
    *argv: str,
    services: CoreServices | None = None,
) -> Run:
    """Call the real :func:`main` with captured streams.

    ``sys.stdout`` and ``sys.stderr`` are deliberately *not* touched here: if
    the entry point ever writes to a stream it was not handed, this helper must
    not be the thing that hides it. ``main`` redirects argparse onto the named
    streams for the same reason.
    """
    out = io.StringIO()
    err = io.StringIO()
    code = entry.main(list(argv), stdout=out, stderr=err, services=services)
    return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture(autouse=True)
def _never_serve_by_accident(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file may reach the real stdio loop.

    ``run_stdio`` takes over the process's stdin and stdout and blocks until a
    client disconnects. A test that reached it would hang the suite rather than
    fail it, so the default is a coroutine that refuses, and the serving tests
    replace it with one that records.
    """

    async def refuse(_services: CoreServices) -> None:  # pragma: no cover - a guard
        raise AssertionError("this test reached the real stdio server")

    monkeypatch.setattr(entry, "run_stdio", refuse)


@pytest.fixture
def recorded_services(monkeypatch: pytest.MonkeyPatch) -> list[CoreServices]:
    """Capture whatever :func:`main` hands to the stdio server, and serve nothing."""
    served: list[CoreServices] = []

    async def record(services: CoreServices) -> None:
        served.append(services)

    monkeypatch.setattr(entry, "run_stdio", record)
    return served


@pytest.fixture
def sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional extra missing, without uninstalling it.

    ``require_sdk`` is the one function that imports it, and this file's whole
    interest is in what the entry point does with its refusal.
    """

    def absent() -> Any:
        raise McpSdkUnavailable()

    monkeypatch.setattr(entry, "require_sdk", absent)


@pytest.fixture
def no_directory_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route to a state directory, wired to fail loudly.

    Used by the tests whose claim is that nothing looked for one. A test that
    only asserted the exit code would pass just as well if the directory had
    been resolved and then ignored.
    """

    def refuse(**_kwargs: object) -> Path:  # pragma: no cover - a guard
        raise AssertionError("a state directory was resolved when none was needed")

    monkeypatch.setattr(entry, "_cli_state_dir", refuse)


# ---------------------------------------------------------------------------
# a running sidecar, and the descriptors that fail before one is reached
# ---------------------------------------------------------------------------


@dataclass
class Sidecar:
    """A live RPC server in a state directory, as the entry point would find one."""

    state_dir: Path
    core: Doubles
    server: RpcServer
    thread: threading.Thread

    @property
    def address(self) -> str:
        return self.server.address

    @property
    def token(self) -> bytes:
        return (runtime_dir(self.state_dir) / TOKEN_FILENAME).read_bytes()

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A temporary directory short enough to bind a Unix socket under.

    ``sun_path`` is 108 bytes on Linux and 104 on macOS, and ``pytest``'s
    ``tmp_path`` already spends most of that on the run counter and the test's
    own name — so a socket under it binds today and stops binding when somebody
    renames the test. Every fixture here that *listens* uses this instead;
    ``tmp_path`` is fine for the descriptor-only cases, which never bind.
    """
    with tempfile.TemporaryDirectory(prefix="pz") as made:
        yield Path(made)


def _serving(state_dir: Path, handler: Callable[[RpcRequest], RpcResponse]) -> RpcServer:
    runtime = runtime_dir(state_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    key = issue_token(runtime)
    server = RpcServer(new_address(runtime), authkey=key, handler=handler)
    write_descriptor(state_dir, server.descriptor())
    return server


@pytest.fixture
def sidecar(short_dir: Path) -> Iterator[Sidecar]:
    """The real router over the fake ports, reachable through a real descriptor."""
    state_dir = short_dir / "pz-agent"
    core = Doubles()
    server = _serving(state_dir, CoreRouter(core.services))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    running = Sidecar(state_dir=state_dir, core=core, server=server, thread=thread)
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def answering(
    short_dir: Path,
) -> Iterator[Callable[[Callable[[RpcRequest], RpcResponse]], Path]]:
    """A sidecar that answers whatever a test says, including things a core cannot.

    ``CoreRouter`` cannot produce an unreadable answer — that is the point of it
    — so the two probe failures that are not "nothing is listening" have to be
    served directly.
    """
    started: list[tuple[RpcServer, threading.Thread]] = []

    def serve(handler: Callable[[RpcRequest], RpcResponse]) -> Path:
        state_dir = short_dir / f"pz-agent-{len(started)}"
        server = _serving(state_dir, handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return state_dir

    try:
        yield serve
    finally:
        for server, thread in started:
            server.close()
            thread.join(timeout=GRACE)


def a_state_dir(tmp_path: Path, name: str = "pz-agent") -> Path:
    """An existing state directory with an empty runtime folder and no descriptor."""
    state_dir = tmp_path / name
    runtime_dir(state_dir).mkdir(parents=True)
    return state_dir


def with_descriptor(state_dir: Path, document: object) -> Path:
    """Write *document* where the descriptor belongs, whatever it is.

    Bypasses :func:`write_descriptor` on purpose: the cases below are files that
    function could not have produced, which is exactly what a truncated write or
    a foreign program leaves behind.
    """
    runtime = runtime_dir(state_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    raw = document if isinstance(document, str) else json.dumps(document)
    (runtime / DESCRIPTOR_FILENAME).write_text(raw, encoding="utf-8")
    return state_dir


def a_dead_pid() -> int:
    """A pid nothing holds.

    Spawned and reaped rather than picked out of the air: a high number is not
    reliably free, and a test that silently pointed at a live process would pass
    for the wrong reason. Borrowed in shape from
    ``tests/unit/test_rpc_token_and_descriptor.py``.
    """
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=30)
    return child.pid


# ---------------------------------------------------------------------------
# the two questions that answer with nothing installed
# ---------------------------------------------------------------------------


class TestTheSurfaceIsReadableWithNothingRunning:
    """``--describe`` and ``--version`` are how a client author starts.

    Their value is entirely in needing nothing: no game, no sidecar, no state
    directory and no optional extra. Each of those is withdrawn here
    individually, because a dependency that crept onto this path would only show
    up on the machine of somebody who had none of it.
    """

    def test_describe_writes_the_catalogue_as_json(self) -> None:
        run = run_main("--describe")

        assert run.code == entry.EXIT_OK
        document = json.loads(run.out)
        assert document["server"] == SERVER_NAME
        assert document["product_version"] == PRODUCT_VERSION
        assert document["tools"], "the described surface publishes no tools"
        assert document["resources"], "the described surface names no resources"
        assert document["protocol_version"], "a client needs a version to key on"

    def test_describe_names_every_tool_and_resource_the_build_declares(self) -> None:
        """The whole catalogue, not the subset one install would publish."""
        document = json.loads(run_main("--describe").out)

        assert len(document["tools"]) == len(TOOLS)
        assert len(document["resources"]) == len(RESOURCES)
        assert document["capability_gated"] is True

    def test_describe_answers_without_the_sdk(self, sdk_absent: None) -> None:
        """The gate that fires first on a plain install must not fire here."""
        run = run_main("--describe")

        assert run.code == entry.EXIT_OK
        assert json.loads(run.out)["tools"]

    def test_describe_reads_no_state_directory(self, no_directory_is_read: None) -> None:
        run = run_main("--describe")

        assert run.code == entry.EXIT_OK

    def test_describe_says_nothing_on_stderr(self) -> None:
        """It is a document, and a client author pipes it into a file."""
        assert run_main("--describe").err == ""

    def test_version_names_the_program_and_the_build(self) -> None:
        run = run_main("--version")

        assert run.code == entry.EXIT_OK
        assert entry.PROGRAM in run.out
        assert PRODUCT_VERSION in run.out

    def test_version_answers_without_the_sdk_or_a_directory(
        self, sdk_absent: None, no_directory_is_read: None
    ) -> None:
        assert run_main("--version").code == entry.EXIT_OK

    def test_help_names_the_server_and_both_directory_flags(self) -> None:
        """``--help`` is the contract a client author reads before the docs."""
        run = run_main("--help")

        assert run.code == entry.EXIT_OK
        assert SERVER_NAME in run.out
        assert "--state-dir" in run.out
        assert "--zomboid-dir" in run.out

    def test_an_unknown_flag_is_a_usage_error_and_stdout_stays_clean(self) -> None:
        run = run_main("--not-a-flag")

        assert run.code == entry.EXIT_USAGE
        assert run.out == "", "argparse wrote its complaint to the protocol stream"
        assert run.err, "a rejected argument with no explanation is not one"


# ---------------------------------------------------------------------------
# two answers to one question
# ---------------------------------------------------------------------------


class TestContradictoryInvocationsAreRefusedRatherThanRanked:
    """A precedence rule here would leave the ignored flag looking honoured."""

    def test_both_directory_flags_together_are_refused(self, tmp_path: Path) -> None:
        state = a_state_dir(tmp_path, "state")
        zomboid = tmp_path / "Zomboid"
        zomboid.mkdir()

        run = run_main("--state-dir", str(state), "--zomboid-dir", str(zomboid))

        assert run.code == entry.EXIT_USAGE
        assert entry.BOTH_DIRECTORIES_MESSAGE in run.err
        assert run.out == ""

    def test_that_refusal_names_both_flags_and_neither_path(self, tmp_path: Path) -> None:
        state = a_state_dir(tmp_path, "state")
        zomboid = tmp_path / "Zomboid"
        zomboid.mkdir()

        run = run_main("--state-dir", str(state), "--zomboid-dir", str(zomboid))

        assert "--state-dir" in run.err
        assert "--zomboid-dir" in run.err
        assert str(state) not in run.err
        assert str(zomboid) not in run.err

    def test_the_contradiction_is_refused_before_the_directories_are_read(
        self, tmp_path: Path, no_directory_is_read: None
    ) -> None:
        """Neither is the answer, so neither is resolved.

        ``--state-dir`` deliberately names a directory that is not there. A
        version that checked each flag's directory *before* noticing the two
        contradict would refuse with "that directory is not there" — and it
        would refuse with :data:`entry.EXIT_USAGE` for it, so the exit code
        cannot tell the two orders apart and the message is what is asserted.
        ``no_directory_is_read`` cannot cover this either: it guards
        :func:`entry._cli_state_dir`, which the ``--state-dir`` branch never
        reaches.
        """
        run = run_main("--state-dir", str(tmp_path / "gone"), "--zomboid-dir", str(tmp_path))

        assert run.code == entry.EXIT_USAGE
        assert entry.BOTH_DIRECTORIES_MESSAGE in run.err
        assert entry.STATE_DIR_MISSING_MESSAGE not in run.err, "a directory was read first"
        assert entry.ZOMBOID_DIR_MISSING_MESSAGE not in run.err, "a directory was read first"

    @pytest.mark.parametrize("flag", ["--state-dir", "--zomboid-dir"])
    def test_supplied_services_and_a_directory_flag_are_refused(
        self, flag: str, tmp_path: Path
    ) -> None:
        """The embedder already answered the question the flag asks."""
        run = run_main(flag, str(tmp_path), services=Doubles().services)

        assert run.code == entry.EXIT_USAGE
        assert entry.SERVICES_AND_DIRECTORY_MESSAGE in run.err
        assert run.out == ""


# ---------------------------------------------------------------------------
# which directory, and where it comes from
# ---------------------------------------------------------------------------


class TestWhichDirectoryIsLookedIn:
    def test_a_state_directory_that_is_not_there_is_a_usage_error(self, tmp_path: Path) -> None:
        run = run_main("--state-dir", str(tmp_path / "nowhere"))

        assert run.code == entry.EXIT_USAGE
        assert entry.STATE_DIR_MISSING_MESSAGE in run.err

    def test_a_zomboid_directory_that_is_not_there_says_which_directory_it_meant(
        self, tmp_path: Path
    ) -> None:
        """Not the same message: one is the game's, one is ours, and a user who
        confused them would go looking in the wrong place."""
        run = run_main("--zomboid-dir", str(tmp_path / "nowhere"))

        assert run.code == entry.EXIT_USAGE
        assert entry.ZOMBOID_DIR_MISSING_MESSAGE in run.err
        assert entry.STATE_DIR_MISSING_MESSAGE not in run.err

    @pytest.mark.parametrize("flag", ["--state-dir", "--zomboid-dir"])
    def test_a_missing_directory_is_named_by_flag_and_never_by_path(
        self, flag: str, tmp_path: Path
    ) -> None:
        """A path carries the account name through the profile directory, and a
        refusal gets pasted into issues."""
        missing = tmp_path / "MyProfile" / "Zomboid"

        run = run_main(flag, str(missing))

        assert flag in run.err
        assert str(missing) not in run.err
        assert "MyProfile" not in run.err

    @needs_a_socket
    def test_the_zomboid_directory_derives_the_one_the_cli_would_use(
        self, short_dir: Path, recorded_services: list[CoreServices]
    ) -> None:
        """The derivation is exercised, not asserted about.

        A sidecar is started under the state directory ``pz-agent`` itself would
        derive from this Zomboid directory, and the entry point is given only
        the Zomboid directory. It connects, or the two halves are deriving
        different directories — which is the failure a copied derivation causes
        and the reason this module imports the CLI's.
        """
        zomboid = short_dir / "Zomboid"
        zomboid.mkdir()
        derived = cli_context.resolve_workspace(
            cli_context.CliContext.from_process().with_overrides(zomboid_dir=zomboid)
        ).state_dir
        server = _serving(derived, CoreRouter(Doubles().services))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            run = run_main("--zomboid-dir", str(zomboid))
        finally:
            server.close()
            thread.join(timeout=GRACE)

        assert run.code == entry.EXIT_OK, run.err
        assert len(recorded_services) == 1

    def test_with_no_flag_the_cli_is_asked_with_no_override_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_services: list[CoreServices]
    ) -> None:
        """The default is the CLI's default, through the CLI's own function.

        The workspace is faked rather than the derivation: what is under test is
        that this process asks, and asks without quietly overriding the game
        directory when the user named neither.
        """
        seen: list[Path | None] = []
        state_dir = a_state_dir(tmp_path)
        real = cli_context.resolve_workspace

        def watched(ctx: cli_context.CliContext) -> cli_context.Workspace:
            seen.append(ctx.discovery.zomboid_dir_override)
            return replace(real(ctx), state_dir=state_dir)

        monkeypatch.setattr(cli_context, "resolve_workspace", watched)

        run = run_main()

        assert seen == [None], "the default path passed an override the user never gave"
        assert run.code == entry.EXIT_NOT_WIRED
        assert entry.NO_DESCRIPTOR_MESSAGE in run.err
        assert recorded_services == []

    def test_an_incomplete_install_says_so_rather_than_guessing_the_layout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI owns where state lives. Absent, this process does not invent it.

        A second copy of the derivation here would agree today and drift later,
        and the two halves would then read and write different directories while
        both looked correct.
        """
        monkeypatch.setitem(sys.modules, "pz_agent_cli.context", None)

        run = run_main()

        assert run.code == entry.EXIT_NO_STATE_DIR
        assert entry.CLI_ABSENT_MESSAGE in run.err
        assert "--state-dir" in run.err, "the refusal does not name the way out"
        assert run.out == ""

    def test_directories_this_machine_cannot_read_are_reported_as_that(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct from an incomplete install in words, though not in code: the
        remedy is the same flag, and the diagnosis is not."""

        def unreadable(_ctx: cli_context.CliContext) -> cli_context.Workspace:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(cli_context, "resolve_workspace", unreadable)

        run = run_main()

        assert run.code == entry.EXIT_NO_STATE_DIR
        assert entry.STATE_DIR_UNDERIVABLE_MESSAGE in run.err
        assert entry.CLI_ABSENT_MESSAGE not in run.err


# ---------------------------------------------------------------------------
# the optional extra
# ---------------------------------------------------------------------------


class TestTheSdkGate:
    def test_a_missing_sdk_is_an_install_step_not_a_traceback(
        self, tmp_path: Path, sdk_absent: None
    ) -> None:
        run = run_main("--state-dir", str(a_state_dir(tmp_path)))

        assert run.code == entry.EXIT_NO_SDK
        assert "pz-agent[mcp]" in run.err
        assert "Traceback" not in run.err
        assert run.out == ""

    def test_the_sdk_is_checked_before_anything_looks_for_a_sidecar(
        self, tmp_path: Path, sdk_absent: None, no_directory_is_read: None
    ) -> None:
        """The common failure on a fresh install is the extra, not the sidecar.

        Reported the other way round, a user with neither installs a game
        component to fix a Python package.
        """
        run = run_main()

        assert run.code == entry.EXIT_NO_SDK

    def test_a_supplied_services_bundle_still_needs_the_sdk(self, sdk_absent: None) -> None:
        """An embedder can supply the ports; nobody can supply the stdio server."""
        run = run_main(services=Doubles().services)

        assert run.code == entry.EXIT_NO_SDK


# ---------------------------------------------------------------------------
# four ways a descriptor stops this process, and four different mornings
# ---------------------------------------------------------------------------


class TestEachDescriptorFailureHasItsOwnRemedy:
    def test_no_descriptor_means_the_sidecar_is_not_running(self, tmp_path: Path) -> None:
        run = run_main("--state-dir", str(a_state_dir(tmp_path)))

        assert run.code == entry.EXIT_NOT_WIRED
        assert entry.NO_DESCRIPTOR_MESSAGE in run.err
        assert "pz-agent start" in run.err
        assert run.out == ""

    def test_no_runtime_folder_at_all_is_the_same_answer(self, tmp_path: Path) -> None:
        """A state directory that exists and was never started in."""
        bare = tmp_path / "pz-agent"
        bare.mkdir()

        run = run_main("--state-dir", str(bare))

        assert run.code == entry.EXIT_NOT_WIRED
        assert entry.NO_DESCRIPTOR_MESSAGE in run.err

    def test_a_descriptor_whose_process_is_gone_is_stale_not_absent(self, tmp_path: Path) -> None:
        """ "Start it" and "find out what keeps killing it" are different days."""
        state_dir = a_state_dir(tmp_path)
        issue_token(runtime_dir(state_dir))
        write_descriptor(
            state_dir,
            RpcDescriptor(
                address=str(runtime_dir(state_dir) / "core-rpc.sock"),
                family=FAMILY_UNIX,
                pid=a_dead_pid(),
            ),
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_STALE_DESCRIPTOR
        assert entry.STALE_DESCRIPTOR_MESSAGE in run.err
        assert entry.NO_DESCRIPTOR_MESSAGE not in run.err

    def test_a_descriptor_that_outlived_its_token_is_stale_too(self, tmp_path: Path) -> None:
        """The shutdown race: the key goes first, and the file is still there."""
        state_dir = a_state_dir(tmp_path)
        write_descriptor(
            state_dir,
            RpcDescriptor(
                address=str(runtime_dir(state_dir) / "core-rpc.sock"),
                family=FAMILY_UNIX,
                pid=os.getpid(),
            ),
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_STALE_DESCRIPTOR

    @pytest.mark.parametrize(
        ("document", "why"),
        [
            ("{not json", "truncated"),
            ('"a string"', "not-an-object"),
            ({"format": "something-else/1", "protocol": RPC_PROTOCOL_VERSION}, "foreign-format"),
            ({"format": "pz-agent-core-rpc-descriptor/1"}, "no-protocol"),
            (
                {"format": "pz-agent-core-rpc-descriptor/1", "protocol": "banana"},
                "unreadable-protocol",
            ),
            (
                {
                    "format": "pz-agent-core-rpc-descriptor/1",
                    "protocol": RPC_PROTOCOL_VERSION,
                    "family": "AF_INET",
                    "address": "127.0.0.1:9",
                    "pid": 1,
                },
                "network-family",
            ),
        ],
        ids=lambda value: value if isinstance(value, str) and " " not in value else "",
    )
    def test_a_file_that_is_not_a_descriptor_asks_for_it_to_be_rewritten(
        self, tmp_path: Path, document: object, why: str
    ) -> None:
        """Not stale: the remedy is to have the file rewritten, not to start a
        process that is already there."""
        state_dir = with_descriptor(a_state_dir(tmp_path, why), document)

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_DESCRIPTOR_UNREADABLE, run.err
        assert entry.DESCRIPTOR_UNREADABLE_MESSAGE in run.err
        assert entry.STALE_DESCRIPTOR_MESSAGE not in run.err

    def test_the_unreadable_refusal_quotes_nothing_out_of_the_file(self, tmp_path: Path) -> None:
        """The file may be one this program did not write, and its contents may
        be anything at all."""
        state_dir = with_descriptor(
            a_state_dir(tmp_path),
            {"format": "some-other-tool/7", "secret": "hunter2", "address": "/tmp/leak.sock"},
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_DESCRIPTOR_UNREADABLE
        assert "hunter2" not in run.err
        assert "some-other-tool" not in run.err
        assert "/tmp/leak.sock" not in run.err

    def test_a_different_protocol_major_names_two_installs(self, tmp_path: Path) -> None:
        """No amount of restarting fixes this, so it must not read like it might."""
        theirs = protocol_major(RPC_PROTOCOL_VERSION) + 41
        state_dir = with_descriptor(
            a_state_dir(tmp_path),
            {
                "format": "pz-agent-core-rpc-descriptor/1",
                "protocol": f"{theirs}.0",
                "family": FAMILY_UNIX,
                "address": "/tmp/x.sock",
                "pid": os.getpid(),
            },
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_PROTOCOL_MISMATCH
        assert str(theirs) in run.err
        assert str(protocol_major(RPC_PROTOCOL_VERSION)) in run.err
        assert entry.DESCRIPTOR_UNREADABLE_MESSAGE not in run.err
        assert "pz-agent start" not in run.err, "restarting cannot fix two installs"

    def test_a_file_far_larger_than_a_descriptor_is_read_only_to_the_cap(
        self, tmp_path: Path
    ) -> None:
        """The classifier reads a bounded prefix, and its answer says so.

        The document below states a Core RPC major this build does not speak, so
        parsing the whole of it would classify it as two installs. It is padded
        past :data:`entry.MAX_DESCRIPTOR_BYTES`, so the prefix that is read is
        not JSON and the answer is that the file is not a descriptor — which is
        also the right answer, because nothing this program writes is eight
        kilobytes. Without the cap this run reports
        :data:`entry.EXIT_PROTOCOL_MISMATCH` and sends a user hunting for a
        second install.
        """
        state_dir = with_descriptor(
            a_state_dir(tmp_path),
            {
                "format": "pz-agent-core-rpc-descriptor/1",
                "protocol": f"{protocol_major(RPC_PROTOCOL_VERSION) + 41}.0",
                "family": FAMILY_UNIX,
                "address": "/tmp/x.sock",
                "pid": os.getpid(),
                "padding": "p" * (entry.MAX_DESCRIPTOR_BYTES * 2),
            },
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_DESCRIPTOR_UNREADABLE, run.err
        assert entry.DESCRIPTOR_UNREADABLE_MESSAGE in run.err
        assert "pppp" not in run.err, "the padding reached the refusal"
        assert run.out == ""

    def test_the_four_descriptor_failures_do_not_share_a_code(self, tmp_path: Path) -> None:
        """The distinction is the point, and it is a property of four *runs*.

        Asserting that the four constants differ would hold with every raise
        site pointing at whichever one it liked; what has to be true is that
        these four state directories leave through four different numbers. That
        the constants are distinct is
        :func:`test_the_exit_codes_are_documented_where_they_are_defined`.
        """
        absent = a_state_dir(tmp_path, "absent")

        stale = a_state_dir(tmp_path, "gone-process")
        issue_token(runtime_dir(stale))
        write_descriptor(
            stale,
            RpcDescriptor(
                address=str(runtime_dir(stale) / "s.sock"),
                family=FAMILY_UNIX,
                pid=a_dead_pid(),
            ),
        )

        mismatch = with_descriptor(
            a_state_dir(tmp_path, "two-installs"),
            {
                "format": "pz-agent-core-rpc-descriptor/1",
                "protocol": f"{protocol_major(RPC_PROTOCOL_VERSION) + 41}.0",
                "family": FAMILY_UNIX,
                "address": "/tmp/x.sock",
                "pid": os.getpid(),
            },
        )
        junk = with_descriptor(a_state_dir(tmp_path, "not-a-descriptor"), "{not json")

        codes = {
            name: run_main("--state-dir", str(state_dir)).code
            for name, state_dir in (
                ("absent", absent),
                ("stale", stale),
                ("mismatch", mismatch),
                ("junk", junk),
            )
        }

        assert len(set(codes.values())) == 4, f"two descriptor failures share an exit code: {codes}"


class TestWhenNothingAnswersOnTheAddress:
    def test_a_live_pid_with_nobody_listening_is_the_sidecar_not_running(
        self, tmp_path: Path
    ) -> None:
        """Every precondition passes and the link still is not there.

        The recorded pid is this process, which is alive, and the token is
        beside the descriptor — so the descriptor checks all succeed and the
        only thing that can find this out is asking.
        """
        state_dir = a_state_dir(tmp_path)
        issue_token(runtime_dir(state_dir))
        write_descriptor(
            state_dir,
            RpcDescriptor(
                address=str(runtime_dir(state_dir) / "nobody.sock"),
                family=FAMILY_UNIX,
                pid=os.getpid(),
            ),
        )

        run = run_main("--state-dir", str(state_dir))

        assert run.code == entry.EXIT_NOT_WIRED
        assert entry.SIDECAR_SILENT_MESSAGE in run.err
        assert run.out == ""

    def test_that_refusal_carries_neither_the_address_nor_the_token(self, tmp_path: Path) -> None:
        state_dir = a_state_dir(tmp_path)
        token = issue_token(runtime_dir(state_dir))
        address = str(runtime_dir(state_dir) / "nobody.sock")
        write_descriptor(
            state_dir,
            RpcDescriptor(address=address, family=FAMILY_UNIX, pid=os.getpid()),
        )

        run = run_main("--state-dir", str(state_dir))

        assert address not in run.err
        assert "nobody.sock" not in run.err
        assert token.hex() not in run.err
        assert token.hex() not in run.out

    @needs_a_socket
    def test_an_answer_this_build_cannot_read_is_not_a_refusal(
        self,
        answering: Callable[[Callable[[RpcRequest], RpcResponse]], Path],
    ) -> None:
        """The core did not say no; this side could not tell what it said.

        Reported as a refusal, a user goes looking for a session precondition
        that is fine. Reported as a dead sidecar, they restart something that is
        running.
        """

        def nonsense(request: RpcRequest) -> RpcResponse:
            return RpcResponse(id=request.id, ok=True, result={"mode": 17})

        run = run_main("--state-dir", str(answering(nonsense)))

        assert run.code == entry.EXIT_ANSWER_UNREADABLE
        assert entry.ANSWER_UNREADABLE_MESSAGE in run.err
        assert entry.SIDECAR_SILENT_MESSAGE not in run.err
        assert run.out == ""

    @needs_a_socket
    def test_the_relayed_detail_is_bounded(
        self,
        answering: Callable[[Callable[[RpcRequest], RpcResponse]], Path],
    ) -> None:
        """Another layer's message is safe to relay and is still not unbounded."""

        def enormous(request: RpcRequest) -> RpcResponse:
            return RpcResponse(
                id=request.id, ok=False, error_code="MALFORMED", error_message="x" * 5000
            )

        run = run_main("--state-dir", str(answering(enormous)))

        assert run.code == entry.EXIT_ANSWER_UNREADABLE
        assert len(run.err) < entry.MAX_RELAYED_DETAIL + len(entry.ANSWER_UNREADABLE_MESSAGE) + 200
        assert "…" in run.err

    @needs_a_socket
    def test_a_core_that_refuses_the_probe_is_still_a_working_link(
        self,
        answering: Callable[[Callable[[RpcRequest], RpcResponse]], Path],
        recorded_services: list[CoreServices],
    ) -> None:
        """A refusal is a completed exchange, and the link is what is under test.

        Refusing to serve here would be this process deciding a session question
        it has strictly less information about than the core that answered it.
        """

        def always_refuse(_request: RpcRequest) -> RpcResponse:
            raise RuntimeError("a verified backup is required")

        run = run_main("--state-dir", str(answering(always_refuse)))

        assert run.code == entry.EXIT_OK, run.err
        assert len(recorded_services) == 1


# ---------------------------------------------------------------------------
# serving, for real
# ---------------------------------------------------------------------------


@needs_a_socket
class TestServingAConnectedSidecar:
    def test_the_ports_handed_to_the_stdio_server_reach_the_running_core(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        """The whole point of the entry point, asserted through the socket.

        Not "a RemoteCoreServices was constructed": the bundle main handed on is
        called, and the value that comes back is the fake core's own.
        """
        run = run_main("--state-dir", str(sidecar.state_dir))

        assert run.code == entry.EXIT_OK, run.err
        assert len(recorded_services) == 1
        assert recorded_services[0].session.status() == sidecar.core.session.snapshot

    def test_every_port_is_wired_not_only_the_probed_one(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        """``session.status`` is what the probe asks. A bundle with one live port
        would satisfy every assertion above it."""
        assert run_main("--state-dir", str(sidecar.state_dir)).code == entry.EXIT_OK
        served = recorded_services[0]

        assert served.observations.latest() == sidecar.core.observations.observation
        assert served.capabilities.report() == sidecar.core.capabilities.report_value
        assert served.diagnostics.doctor() == sidecar.core.diagnostics.checks

    def test_nothing_at_all_reaches_stdout(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        """From here on stdout is the client's JSON-RPC stream."""
        run = run_main("--state-dir", str(sidecar.state_dir))

        assert run.out == ""

    def test_the_one_line_on_stderr_names_the_server_and_the_build(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        run = run_main("--state-dir", str(sidecar.state_dir))

        assert run.err.count("\n") == 1, "the connection line is not one line"
        assert SERVER_NAME in run.err
        assert PRODUCT_VERSION in run.err

    def test_neither_the_address_nor_the_token_reaches_either_stream(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        """A support bundle, a screenshot and a bug report are exactly where the
        sidecar's private door and the key to it must not turn up."""
        token = sidecar.token
        address = sidecar.address

        run = run_main("--state-dir", str(sidecar.state_dir))

        for stream in (run.out, run.err):
            assert address not in stream
            assert Path(address).name not in stream
            assert token.hex() not in stream
            assert str(sidecar.state_dir) not in stream

    def test_the_probe_happens_before_serving_starts(
        self, sidecar: Sidecar, recorded_services: list[CoreServices]
    ) -> None:
        """A client that gets a clean handshake and a failure on every tool call
        sees a working server with broken tools. Exiting with a reason shows the
        reason instead."""
        sidecar.stop()

        run = run_main("--state-dir", str(sidecar.state_dir))

        assert run.code == entry.EXIT_NOT_WIRED
        assert recorded_services == [], "serving began without a link"


class TestAnEmbedderSuppliesItsOwnPorts:
    def test_supplied_services_are_served_as_they_are(
        self, recorded_services: list[CoreServices]
    ) -> None:
        services = Doubles().services

        run = run_main(services=services)

        assert run.code == entry.EXIT_OK
        assert recorded_services == [services]

    def test_no_directory_is_resolved_and_no_descriptor_is_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_directory_is_read: None,
        recorded_services: list[CoreServices],
    ) -> None:
        """The embedder answered the question the directory flags ask."""

        def refuse(*_args: object, **_kwargs: object) -> None:  # pragma: no cover - a guard
            raise AssertionError("a remote link was built for supplied services")

        monkeypatch.setattr(RemoteCoreServices, "from_state_dir", refuse)

        assert run_main(services=Doubles().services).code == entry.EXIT_OK
        assert len(recorded_services) == 1

    def test_the_connection_line_is_not_written_for_supplied_ports(
        self, recorded_services: list[CoreServices]
    ) -> None:
        """There was no connection. Saying there was would be a fabricated
        postcondition on the one line a client shows its user."""
        run = run_main(services=Doubles().services)

        assert "connected to the sidecar" not in run.err


# ---------------------------------------------------------------------------
# properties that hold across every path
# ---------------------------------------------------------------------------


#: The refusals, by name, for parametrising. Written out rather than derived
#: from :func:`_every_refusal`, because deriving them would mean building state
#: directories at *collection* time — under whatever path the decorator was
#: handed, before any fixture exists. :func:`test_the_refusal_cases_are_all_of
#: _them` is what keeps the two in step.
REFUSAL_CASES: Final = (
    "both-flags",
    "absent-state-dir",
    "absent-zomboid-dir",
    "no-descriptor",
    "stale",
    "mismatch",
    "unreadable",
    "bad-flag",
)


def _every_refusal(tmp_path: Path) -> dict[str, list[str]]:
    """One invocation per refusal, each reaching a different branch."""
    stale = a_state_dir(tmp_path, "stale")
    issue_token(runtime_dir(stale))
    write_descriptor(
        stale,
        RpcDescriptor(
            address=str(runtime_dir(stale) / "s.sock"), family=FAMILY_UNIX, pid=a_dead_pid()
        ),
    )
    mismatch = with_descriptor(
        a_state_dir(tmp_path, "mismatch"),
        {
            "format": "pz-agent-core-rpc-descriptor/1",
            "protocol": f"{protocol_major(RPC_PROTOCOL_VERSION) + 41}.0",
            "family": FAMILY_UNIX,
            "address": "/tmp/x.sock",
            "pid": os.getpid(),
        },
    )
    return {
        "both-flags": ["--state-dir", str(tmp_path), "--zomboid-dir", str(tmp_path)],
        "absent-state-dir": ["--state-dir", str(tmp_path / "gone")],
        "absent-zomboid-dir": ["--zomboid-dir", str(tmp_path / "gone")],
        "no-descriptor": ["--state-dir", str(a_state_dir(tmp_path, "empty"))],
        "stale": ["--state-dir", str(stale)],
        "mismatch": ["--state-dir", str(mismatch)],
        "unreadable": ["--state-dir", str(with_descriptor(a_state_dir(tmp_path, "junk"), "{"))],
        "bad-flag": ["--nope"],
    }


def test_the_refusal_cases_are_all_of_them(tmp_path: Path) -> None:
    """The parametrised list and the builder cannot drift apart silently."""
    assert set(_every_refusal(tmp_path)) == set(REFUSAL_CASES)


@pytest.mark.parametrize("case", REFUSAL_CASES)
def test_no_refusal_writes_to_the_protocol_stream(case: str, tmp_path: Path) -> None:
    """One stray line on stdout corrupts the session for every client.

    Parametrised over every refusal rather than asserted once, because the check
    is cheap and the failure is silent: a client sees a protocol error, not a
    diagnostic.
    """
    argv = _every_refusal(tmp_path)[case]

    run = run_main(*argv)

    assert run.out == "", f"{case} wrote to stdout"
    assert run.err.strip(), f"{case} refused without saying why"
    assert run.code != entry.EXIT_OK


def test_every_refusal_names_a_command_or_a_flag_to_run_next(tmp_path: Path) -> None:
    """A refusal that only says what failed leaves the user where they started."""
    remedies = ("pz-agent", "--state-dir", "--zomboid-dir", "pip install", "usage:")
    for case, argv in _every_refusal(tmp_path).items():
        run = run_main(*argv)

        assert any(remedy in run.err for remedy in remedies), f"{case} names no remedy"


def test_main_returns_an_int_for_every_refusal_and_never_raises(tmp_path: Path) -> None:
    """`cli` turns the value into an exit status, so a raise would become a
    traceback on the stream a client is reading."""
    for argv in _every_refusal(tmp_path).values():
        assert isinstance(run_main(*argv).code, int)


def test_the_console_script_exits_with_what_main_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one line between :func:`main` and the process's status."""
    monkeypatch.setattr(entry, "main", lambda: entry.EXIT_STALE_DESCRIPTOR)

    with pytest.raises(SystemExit) as left:
        entry.cli()

    assert left.value.code == entry.EXIT_STALE_DESCRIPTOR


def test_the_exit_codes_are_documented_where_they_are_defined() -> None:
    """Every code this module publishes is exported and distinct.

    ``__all__`` is what a contract test and a downstream client import through;
    a code defined and not exported is one nobody can branch on.
    """
    exported = {name for name in entry.__all__ if name.startswith("EXIT_")}
    defined = {name for name in vars(entry) if name.startswith("EXIT_")}

    assert exported == defined
    assert len({getattr(entry, name) for name in defined}) == len(defined)


def test_every_refusal_message_is_exported(tmp_path: Path) -> None:
    """A message a test cannot import is one a test asserts by retyping it."""
    messages = {name for name in vars(entry) if name.endswith("_MESSAGE")}

    assert messages <= set(entry.__all__)
