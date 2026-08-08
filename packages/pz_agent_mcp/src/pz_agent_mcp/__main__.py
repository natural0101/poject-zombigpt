"""Console entry point for ``pz-agent-mcp`` (and for ``python -m pz_agent_mcp``).

The module ``pyproject.toml`` points its console script at. It owns no domain
logic at all — four decisions and the exit code each one produces:

* **The SDK is checked before anything to do with the sidecar.** ``mcp`` is an
  optional extra, so the common failure on a fresh install is that it is simply
  not there. That is reported as the install step it is — one line naming the
  extra — and never as an ImportError traceback, which tells a user what Python
  was doing rather than what they have to do.
* **Serving needs a core, and this process is not the one that owns it.** An MCP
  client launches this executable as a subprocess, so the session, the
  observation store, the action engine, the capability report and the memory are
  all in the sidecar. :class:`~.remote.client.RemoteCoreServices` reaches them
  over the Local Core RPC link (``docs/CORE_RPC.md``), built from a state
  directory and nothing else. This module's job is to work out *which* state
  directory, and to say precisely why it cannot reach a core when it cannot.
* **Which refusal it is decides what the user does next.** "No sidecar is
  running", "the sidecar died without cleaning up", "there are two installs on
  this machine" and "the SDK extra is missing" have four different remedies, so
  they have four different exit codes and four different messages. Collapsing
  any pair of them sends somebody to fix the wrong thing, which costs an
  evening.
* **The catalogue is answerable without any of it.** ``--describe`` writes the
  whole published surface as JSON, and ``--version`` names the build. Neither
  needs the SDK, a game, a sidecar or a state directory — which is what makes
  them the pair a client author reads first, and why nothing on their path is
  allowed to grow a dependency on the rest.

Where the state directory comes from
------------------------------------

``--state-dir`` names it outright. That is what ``pz-agent start`` prints into
the client configuration it tells the user to paste, so it is the path a working
install takes.

``--zomboid-dir`` names the game's user directory and the state directory is
derived from it. The derivation is *imported* from
:mod:`pz_agent_cli.context` rather than copied: it is one line today
(``<Zomboid>/pz-agent``), but it also honours ``[game]`` in ``config.toml``, and
a second copy of it here would drift the first time either changed — with the
two processes then reading and writing different directories while both looked
correct. That import is deferred, because it pulls in the whole CLI and neither
``--describe`` nor ``--version`` may depend on it.

Neither flag: the same default, through the same function, with no override.

Both flags: refused. They are two answers to one question, and a precedence rule
would mean the flag a user thought they were setting is silently the one that
does nothing.

Nothing reaches stdout but the protocol
---------------------------------------

Once serving starts, stdout is the JSON-RPC stream an MCP client is parsing, and
a single stray line on it corrupts the session for every client. So every
diagnostic this module writes goes to stderr, and the one line it writes on a
successful connection names the server and the version and nothing else. The
address and the token never reach either stream: the address is the sidecar's
private door and the token is the key to it, and a support bundle, a screenshot
or a bug report is exactly where they must not turn up.

:func:`main` returns an exit code and never calls :func:`sys.exit`, so a test
drives the real entry point in this process and reads exactly what a user would
have seen. Argparse's own output — ``--help``, ``--version`` and a rejected
argument — is redirected onto the same two streams for the same reason: a stream
argument that three invocations quietly ignored would be worse than none.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Final, TextIO

from pz_agent_core.protocol import JsonDict
from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    DescriptorError,
    StaleDescriptor,
    descriptor_path,
    load_descriptor,
)
from pz_agent_core.rpc.transport import DEFAULT_DEADLINE_SECONDS
from pz_agent_core.version import (
    PRODUCT_VERSION,
    PROTOCOL_VERSION,
    RPC_PROTOCOL_VERSION,
    protocol_major,
)

from .catalog import RESOURCES, TOOLS
from .ports import CoreServices
from .remote.client import (
    CoreAnswerUnreadable,
    CoreRefused,
    RemoteCoreServices,
    SidecarUnavailable,
)
from .server import (
    SERVER_NAME,
    McpSdkIncompatible,
    McpSdkUnavailable,
    require_sdk,
    run_stdio,
)

__all__ = [
    "ANSWER_UNREADABLE_MESSAGE",
    "BOTH_DIRECTORIES_MESSAGE",
    "CLI_ABSENT_MESSAGE",
    "DESCRIPTOR_UNREADABLE_MESSAGE",
    "EXIT_ANSWER_UNREADABLE",
    "EXIT_DESCRIPTOR_UNREADABLE",
    "EXIT_NOT_WIRED",
    "EXIT_NO_SDK",
    "EXIT_NO_STATE_DIR",
    "EXIT_OK",
    "EXIT_PROTOCOL_MISMATCH",
    "EXIT_SDK_INCOMPATIBLE",
    "EXIT_SERVER_FAILED",
    "EXIT_STALE_DESCRIPTOR",
    "EXIT_USAGE",
    "NO_DESCRIPTOR_MESSAGE",
    "PROGRAM",
    "SERVICES_AND_DIRECTORY_MESSAGE",
    "SIDECAR_SILENT_MESSAGE",
    "STALE_DESCRIPTOR_MESSAGE",
    "STATE_DIR_MISSING_MESSAGE",
    "STATE_DIR_UNDERIVABLE_MESSAGE",
    "ZOMBOID_DIR_MISSING_MESSAGE",
    "build_parser",
    "catalogue_document",
    "cli",
    "main",
]

PROGRAM: Final = "pz-agent-mcp"

#: Served the surface, or answered a question about it.
EXIT_OK: Final = 0

#: The command was understood and there is no core to serve through: no
#: descriptor where one would be, or a live descriptor whose address nothing
#: answers on. Both mean the sidecar is not running and both are fixed by
#: starting it, which is why they share a code — the same reason
#: :class:`~.remote.client.SidecarUnavailable` is one class rather than six.
#:
#: The number is deliberately the one this program has always returned when it
#: had nothing to serve. It used to mean "no build of this exists that can reach
#: a sidecar at all"; it now means "the sidecar this one would reach is not
#: running". ``configs/mcp/README.md`` documents exit 1 as the refusal you meet
#: when the SDK is present and the core is not, and that stays true.
EXIT_NOT_WIRED: Final = 1

#: A malformed invocation. Matches argparse's own, and the CLI's.
EXIT_USAGE: Final = 2

#: The optional ``mcp`` extra is not installed. Distinct from every other
#: failure because the remedy is a single install command.
EXIT_NO_SDK: Final = 3

#: A descriptor is there, and the process it names is gone — or its token went
#: with it. The sidecar was killed rather than stopped. Distinct from
#: :data:`EXIT_NOT_WIRED` because a file that outlived its process is worth
#: saying out loud: it is the difference between "start it" and "it started,
#: and something is stopping it".
EXIT_STALE_DESCRIPTOR: Final = 4

#: The sidecar speaks a different Core RPC major than this executable does. Both
#: halves ship from one install, so this means two installs are present, and no
#: amount of restarting fixes it.
EXIT_PROTOCOL_MISMATCH: Final = 5

#: There is a file where the descriptor belongs and it is not one: truncated,
#: foreign, or from a start that did not finish. Distinct from stale because the
#: remedy is to have it rewritten, not to start a process that is already there.
EXIT_DESCRIPTOR_UNREADABLE: Final = 6

#: Something answered on the sidecar's address and the answer is not one this
#: build can read. Never reported as a refusal: the core did not say no, this
#: side could not tell what it said.
EXIT_ANSWER_UNREADABLE: Final = 7

#: Neither flag was given and this process cannot work out which state directory
#: to use — the package that owns the layout is not importable, or the machine's
#: directories could not be read. One code because there is one remedy: name the
#: directory with ``--state-dir``.
EXIT_NO_STATE_DIR: Final = 8

#: The SDK is installed and is not a version this build can drive. Distinct from
#: :data:`EXIT_NO_SDK` because the remedy is a version constraint rather than an
#: install, and because the two were indistinguishable while this failure went
#: unnoticed: `pyproject.toml` asked for `mcp>=1.2` with no upper bound, 2.0
#: resolved, its `Server` had dropped the four registration methods this build
#: called, and the child died with an AttributeError traceback carrying no code
#: at all. A traceback is not a diagnosis.
EXIT_SDK_INCOMPATIBLE: Final = 9

#: The server itself failed — an exception escaped :func:`build_server` or the
#: serve loop after every named refusal above had its chance. This code exists
#: because the alternative was Python's generic 1 under a traceback (R-002):
#: :func:`require_sdk` checks the constructor's four keywords, but an SDK can
#: keep the signature and change behind it, and a catalogue defect surfaces
#: only when the server is actually built. A client author reads one line on
#: stderr and this code, not a stack.
EXIT_SERVER_FAILED: Final = 10

#: Descriptors are a few hundred bytes. The cap is on the read rather than on
#: what follows it, so a huge file where a descriptor belongs costs one bounded
#: read instead of a parse.
MAX_DESCRIPTOR_BYTES: Final = 8 * 1024

#: How much of another layer's message is relayed. The Core RPC layer's errors
#: name a field and a reason and never a value (``docs/CORE_RPC.md``), so
#: relaying one is safe — but "bounded everything" includes the length of a line
#: written to a stream somebody else is reading.
MAX_RELAYED_DETAIL: Final = 300

#: Two answers to one question. Neither wins: a precedence rule would leave a
#: user certain they had set the directory that was in fact ignored.
BOTH_DIRECTORIES_MESSAGE: Final = (
    "--state-dir and --zomboid-dir both name where to look, and they can disagree. "
    "Give one: --state-dir names the sidecar's state directory outright, and "
    "--zomboid-dir names the game's user directory to derive it from. "
    "'pz-agent start' prints the client configuration to paste, with --state-dir already in it."
)

#: An embedder that passes its own ports has already answered the question the
#: directory flags ask.
SERVICES_AND_DIRECTORY_MESSAGE: Final = (
    "the caller supplied core services, and --state-dir or --zomboid-dir asks this process to "
    "find its own. Pass one or the other: services=... serves those ports and reads no "
    "directory at all."
)

STATE_DIR_MISSING_MESSAGE: Final = (
    "--state-dir names a directory that is not there, so there is nothing to read. "
    "'pz-agent start' prints the client configuration to paste, with the state directory "
    "this machine uses already in it."
)

ZOMBOID_DIR_MISSING_MESSAGE: Final = (
    "--zomboid-dir names a directory that is not there. It is the game's user directory — "
    "the one holding Saves and Lua — and 'pz-agent doctor' reports where this machine's is."
)

#: Both causes end at the same instruction, which is why they share a code.
CLI_ABSENT_MESSAGE: Final = (
    "the pz-agent command-line package, which owns where the state directory lives, is not "
    "importable, so it cannot be asked. Both halves ship in one distribution, so this install "
    "is incomplete: reinstall it, or give --state-dir to name the directory directly."
)

STATE_DIR_UNDERIVABLE_MESSAGE: Final = (
    "this machine's directories could not be read, so the state directory could not be "
    "derived from them. Give --state-dir to name it directly; 'pz-agent doctor' reports what "
    "is unreadable."
)

NO_DESCRIPTOR_MESSAGE: Final = (
    f"no sidecar is running for that state directory: its runtime folder holds no "
    f"{DESCRIPTOR_FILENAME}, and that file is the only thing that names the address to call. "
    "Start it with 'pz-agent start', then check it with 'pz-agent status'."
)

STALE_DESCRIPTOR_MESSAGE: Final = (
    f"{DESCRIPTOR_FILENAME} is there, but the process it names is gone or its token file went "
    "with it: the sidecar stopped without cleaning up. Start it again with 'pz-agent start'; "
    "if it keeps stopping, 'pz-agent doctor' reports why."
)

DESCRIPTOR_UNREADABLE_MESSAGE: Final = (
    f"{DESCRIPTOR_FILENAME} is not a descriptor this build can read — truncated, written by "
    "another program, or left by a start that did not finish. 'pz-agent stop' followed by "
    "'pz-agent start' rewrites it; 'pz-agent doctor' reports what is in the way."
)

SIDECAR_SILENT_MESSAGE: Final = (
    f"{DESCRIPTOR_FILENAME} names a live process, but nothing answered on the address it gives "
    f"within {DEFAULT_DEADLINE_SECONDS:.0f} seconds. Restart the sidecar with 'pz-agent stop' "
    "then 'pz-agent start', and check it with 'pz-agent status'."
)

ANSWER_UNREADABLE_MESSAGE: Final = (
    "the sidecar answered and this build cannot read the answer, so the state of the session "
    "is unknown rather than bad. Both halves ship from one install: run 'pz-agent --version' "
    f"and '{PROGRAM} --version' and use the pair that match, then 'pz-agent doctor'."
)


def _protocol_mismatch_message(theirs: int, ours: int) -> str:
    """Named with both majors, because which is older is what a user acts on."""
    return (
        f"{DESCRIPTOR_FILENAME} says the sidecar speaks Core RPC protocol major {theirs} and "
        f"this build speaks {ours}. Both halves ship from one install, so two installs are "
        f"present: run 'pz-agent --version' and '{PROGRAM} --version' and use the pair that "
        "match. Restarting the sidecar cannot fix this."
    )


class _Refused(Exception):
    """A refusal, carrying the exit code that names which one it is.

    Private, and never crosses this module's boundary: :func:`main` catches it,
    writes its message to stderr and returns its code. It exists so the checks
    below read as one sequence of preconditions rather than as a chain of
    functions each returning a value-or-error that the next has to unpack.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. One place, so ``--help`` is the contract."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="stdio MCP server for pz-agent",
        epilog=(
            "Configure it in an MCP client as: "
            '{"command": "python", "args": ["-m", "pz_agent_mcp", "--state-dir", "PATH"]}. '
            "'pz-agent start' prints that block with this machine's directory already in it. "
            "The server name it registers under is " + SERVER_NAME + "."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {PRODUCT_VERSION}")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="write the published tools and resources as JSON and exit",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "the sidecar's state directory; its runtime folder holds the "
            f"{DESCRIPTOR_FILENAME} this server connects through"
        ),
    )
    parser.add_argument(
        "--zomboid-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "the game's user directory, from which the state directory is derived the way "
            "pz-agent derives it. Not to be combined with --state-dir"
        ),
    )
    return parser


def catalogue_document() -> JsonDict:
    """Every tool and resource this build declares, with the versions behind them.

    The *whole* catalogue, not the subset a particular install publishes: which
    tools are withheld depends on the capability report, which belongs to a
    session this process does not have. A reader wanting the ready set asks a
    running server, which filters through
    :func:`~.catalog.published_tools`.
    """
    return {
        "server": SERVER_NAME,
        "product_version": PRODUCT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "capability_gated": True,
        "tools": [spec.descriptor() for spec in TOOLS],
        "resources": [spec.descriptor() for spec in RESOURCES],
    }


def _clamped(detail: str) -> str:
    """Another layer's message, bounded before it is written anywhere."""
    if len(detail) <= MAX_RELAYED_DETAIL:
        return detail
    return detail[:MAX_RELAYED_DETAIL] + "…"


def _refuse_contradictions(args: argparse.Namespace, *, services: CoreServices | None) -> None:
    """Refuse the two invocations that answer one question twice."""
    if args.state_dir is not None and args.zomboid_dir is not None:
        raise _Refused(EXIT_USAGE, BOTH_DIRECTORIES_MESSAGE)
    if services is not None and (args.state_dir is not None or args.zomboid_dir is not None):
        raise _Refused(EXIT_USAGE, SERVICES_AND_DIRECTORY_MESSAGE)


def _require_sdk() -> None:
    """The optional extra, as an install step rather than a traceback."""
    try:
        require_sdk()
    except McpSdkUnavailable as absent:
        raise _Refused(EXIT_NO_SDK, str(absent)) from absent
    except McpSdkIncompatible as mismatched:
        raise _Refused(EXIT_SDK_INCOMPATIBLE, str(mismatched)) from mismatched


def _existing(directory: Path, message: str) -> Path:
    """*directory*, or a refusal naming the flag that pointed at nothing.

    The flag is named and its value is not. A path is the one string in this
    program that carries the account name through the profile directory, and a
    refusal gets pasted into issues and support bundles; the user typed the
    value and can read it back off their own command line.
    """
    if not directory.is_dir():
        raise _Refused(EXIT_USAGE, message)
    return directory


def _cli_state_dir(*, zomboid_dir: Path | None) -> Path:
    """The state directory ``pz-agent`` itself would use, asked rather than copied.

    One derivation for both processes. It is not simply ``<Zomboid>/pz-agent``:
    it also honours ``[game]`` in ``config.toml``, and falls back to a directory
    beside the working directory when no game profile is found at all. A copy
    here would agree with that today and drift from it later, and the two halves
    would then be reading different directories while each looked right.
    """
    try:
        # Deferred: this pulls in the whole CLI, and `--describe` and `--version`
        # answer on installs where nothing else works.
        from pz_agent_cli.context import CliContext, resolve_workspace  # noqa: PLC0415
    except ImportError as incomplete:
        raise _Refused(EXIT_NO_STATE_DIR, CLI_ABSENT_MESSAGE) from incomplete

    context = CliContext.from_process()
    if zomboid_dir is not None:
        context = context.with_overrides(zomboid_dir=zomboid_dir)
    try:
        return resolve_workspace(context).state_dir
    except OSError as unreadable:
        raise _Refused(EXIT_NO_STATE_DIR, STATE_DIR_UNDERIVABLE_MESSAGE) from unreadable


def _state_dir(args: argparse.Namespace) -> Path:
    """Which directory this process will look in, from the flags it was given."""
    if args.state_dir is not None:
        return _existing(args.state_dir, STATE_DIR_MISSING_MESSAGE)
    if args.zomboid_dir is not None:
        zomboid = _existing(args.zomboid_dir, ZOMBOID_DIR_MISSING_MESSAGE)
        return _cli_state_dir(zomboid_dir=zomboid)
    return _cli_state_dir(zomboid_dir=None)


def _stated_protocol_major(state_dir: Path) -> int | None:
    """The Core RPC major the descriptor claims, if it claims a readable one.

    A classifier, not a second loader: :func:`load_descriptor` has already
    refused by the time this is called, and this only decides *which* of its
    refusals it was. The type cannot say — a protocol mismatch and a foreign
    file are both ``DescriptorError`` — and the two remedies are opposite ends
    of the day: one install to remove, or one file to have rewritten.
    """
    try:
        with descriptor_path(state_dir).open("rb") as handle:
            raw = handle.read(MAX_DESCRIPTOR_BYTES)
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Unreadable at this point means unreadable, which is exactly the answer
        # the caller falls back to; there is nothing to report separately.
        return None
    if not isinstance(document, dict):
        return None
    protocol = document.get("protocol")
    if not isinstance(protocol, str):
        return None
    try:
        return protocol_major(protocol)
    except ValueError:
        # A protocol field that is not a version is a malformed descriptor
        # rather than a different build, and is reported as one.
        return None


def _refuse_unusable_descriptor(state_dir: Path) -> None:
    """Every precondition for connecting, each with its own answer.

    Checked before a client is built so that the four causes reach the user as
    four exit codes. :func:`load_descriptor` collapses two of them —
    "there is no descriptor" and "the descriptor is stale" are both
    :class:`StaleDescriptor`, because to it they are one fact — and this is the
    caller that has to tell them apart, since one means start the sidecar and
    the other means find out what keeps killing it.
    """
    if not descriptor_path(state_dir).exists():
        raise _Refused(EXIT_NOT_WIRED, NO_DESCRIPTOR_MESSAGE)
    try:
        load_descriptor(state_dir)
    except StaleDescriptor as stale:
        # The file can also vanish between the check above and this call — a
        # sidecar shutting down does exactly that — so which case it is comes
        # from looking again rather than from the exception type.
        if not descriptor_path(state_dir).exists():
            raise _Refused(EXIT_NOT_WIRED, NO_DESCRIPTOR_MESSAGE) from stale
        raise _Refused(EXIT_STALE_DESCRIPTOR, STALE_DESCRIPTOR_MESSAGE) from stale
    except DescriptorError as unusable:
        # Deliberately not relaying this message: it quotes the offending file's
        # own `format` value, and the file may be one this program did not
        # write. The classification below is what the user acts on.
        theirs = _stated_protocol_major(state_dir)
        ours = protocol_major(RPC_PROTOCOL_VERSION)
        if theirs is not None and theirs != ours:
            raise _Refused(
                EXIT_PROTOCOL_MISMATCH, _protocol_mismatch_message(theirs, ours)
            ) from unusable
        raise _Refused(EXIT_DESCRIPTOR_UNREADABLE, DESCRIPTOR_UNREADABLE_MESSAGE) from unusable


def _probe(services: CoreServices) -> None:
    """One real call, so that "connected" is observed rather than assumed.

    A descriptor that passes every check still only proves a process is alive
    at a recorded pid. Asking it something is what proves the link, and this
    process has to know before it starts serving: an MCP client that gets a
    successful handshake and then a failure on every tool call shows the user a
    working server with broken tools, while a process that exits with a reason
    shows them the reason.

    ``session.status`` because it is the cheapest thing the core can be asked
    and it changes nothing.
    """
    try:
        services.session.status()
    except CoreRefused:
        # The core answered, and its answer was no. That is a completed
        # exchange — the link is what is under test here — and the refusal
        # belongs to whichever call meets it, reported through the tool result
        # that carries a reason code. Discarding it here loses nothing.
        return
    except SidecarUnavailable as silent:
        raise _Refused(EXIT_NOT_WIRED, SIDECAR_SILENT_MESSAGE) from silent
    except CoreAnswerUnreadable as unreadable:
        raise _Refused(
            EXIT_ANSWER_UNREADABLE,
            f"{ANSWER_UNREADABLE_MESSAGE} ({_clamped(str(unreadable))})",
        ) from unreadable


def _connected_services(args: argparse.Namespace, err: TextIO) -> CoreServices:
    """The sidecar's ports, over a link this function has seen answer.

    Nothing is cached inside them: the descriptor and the token are resolved per
    call, so a sidecar that restarts while this process is serving is reachable
    again without the MCP client restarting anything.
    """
    state_dir = _state_dir(args)
    _refuse_unusable_descriptor(state_dir)
    services = RemoteCoreServices.from_state_dir(state_dir, deadline=DEFAULT_DEADLINE_SECONDS)
    _probe(services)
    # stderr, and no address, no token and no path: stdout is the client's
    # JSON-RPC stream from here on, and this line is a log entry in whatever the
    # client shows its user.
    err.write(f"{PROGRAM}: connected to the sidecar; serving {SERVER_NAME} {PRODUCT_VERSION}\n")
    return services


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    services: CoreServices | None = None,
) -> int:
    """Run the entry point and return its exit code. Never raises for a user error.

    Args:
        argv: the command line, without the program name. ``None`` reads
            :data:`sys.argv`, as argparse does.
        stdout: where ``--describe`` writes. Never written to on the serving
            path, where it belongs to the protocol.
        stderr: where every refusal and the one connection line go.
        services: an embedder's own ports. Given, they are served as they are
            and no directory is read; the two directory flags are then refused
            rather than ignored.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    parser = build_parser()
    try:
        # argparse writes `--help`, `--version` and its own usage errors to the
        # process-wide streams, looked up at the moment it writes. Redirected so
        # that *every* byte this function produces goes to the streams the caller
        # named — otherwise `main(stdout=...)` would be a promise with three
        # exceptions to it, and a test that captured only the named stream would
        # report an empty stdout for an invocation that had written to the real
        # one.
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as exit_request:
        # --help and --version leave through here having already written their
        # answer; so does a rejected argument. Returning the code keeps the
        # promise that this function does not raise.
        code = exit_request.code
        return code if isinstance(code, int) else EXIT_USAGE

    if args.describe:
        out.write(json.dumps(catalogue_document(), ensure_ascii=False, indent=2) + "\n")
        return EXIT_OK

    try:
        _refuse_contradictions(args, services=services)
        _require_sdk()
        attached = services if services is not None else _connected_services(args, err)
    except _Refused as refusal:
        err.write(f"{PROGRAM}: {refusal}\n")
        return refusal.code

    try:
        asyncio.run(run_stdio(attached))
    # The one broad handler in this module: past every named refusal, this is
    # the boundary where a traceback would otherwise reach a client author.
    except Exception as crashed:
        err.write(
            f"{PROGRAM}: the server failed while serving: "
            f"{type(crashed).__name__}: {_clamped(str(crashed))}\n"
        )
        return EXIT_SERVER_FAILED
    return EXIT_OK


def cli() -> None:
    """Console-script wrapper: run :func:`main` and exit with its code."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
