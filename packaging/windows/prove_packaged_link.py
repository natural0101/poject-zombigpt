#!/usr/bin/env python3
"""Prove the packaged pair completes an MCP initialize over the real RPC link.

``--version`` and ``--describe`` prove that each frozen executable imports its
own bundle; neither proves the two of them can talk. The link between them is
real machinery: ``pz-agent start --foreground`` attaches a session, serves the
Core RPC endpoint and publishes ``runtime/core-rpc.json``, and ``pz-agent-mcp
--state-dir`` reads that descriptor, dials the address, probes the core, and
only then serves MCP over stdio. This driver runs that whole chain using the
shipped surface only, and succeeds solely on the one observation that matters:
a JSON-RPC ``initialize`` *result* carrying ``serverInfo``, read off the MCP
child's stdout before a hard deadline.

**Command mechanism, chosen once.** ``--pz-agent`` and ``--pz-agent-mcp`` are
repeatable flags, one argv token per occurrence: a packaged binary is one
occurrence (``--pz-agent dist/pz-agent.exe``) and an interpreter form is three
(``--pz-agent=python --pz-agent=-m --pz-agent=pz_agent_cli``). Repetition was
picked over shlex because it needs no quoting rules at all, and quoting is
exactly where a Windows path full of backslashes and a POSIX command line
disagree. The ``=`` spelling is required for a token that begins with a dash,
because argparse otherwise reads ``-m`` as an option of this program's own.

**The sidecar is driven the shipped way.** The global ``--state-dir``,
``--zomboid-dir`` and ``--config`` flags, then ``start --foreground`` — the
same recipe ``pz_agent_cli.app._sidecar_argv`` composes when ``start``
detaches a child of its own. The configuration is a copy of the shipped
``configs/agent/config.example.toml``: the CLI has no command that writes a
configuration file, and the product's own missing-config remediation — the
copy ``install.bat`` performs in the release archive — is to put exactly that
file into place, so this driver performs the same shipped step rather than
inventing a document of its own.

**No product import.** The subject under proof is the packaged executable, so
this driver must not answer any question by importing the packages that happen
to be importable where it runs. The two file names it knows — ``runtime`` and
``core-rpc.json`` — mirror ``RUNTIME_DIRNAME`` and ``DESCRIPTOR_FILENAME`` in
``pz_agent_core.rpc.descriptor``, which documents them as the published
contract an MCP client resolves the sidecar through.

Everything here is bounded: the descriptor poll, the stdout read (a reader
thread feeding a queue that is polled under a deadline, never a bare blocking
read), and every child wait, with a kill on overrun. A failure exits nonzero
with a one-line refusal naming the phase that failed and what was seen. The
auth token and the descriptor address are never read and never printed; a
refusal quotes at most a clamped tail of a child's stderr, which the product
keeps free of both.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: Exit code for the observed success, and the single one for any refusal. The
#: phases are told apart by the printed line, not by a code table: this driver
#: has exactly one caller shape — a CI step or a test reading one line — and a
#: numeric vocabulary nobody consumes would be a contract nothing keeps.
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1

#: How long the sidecar gets to publish the RPC descriptor, and how often the
#: file is looked for. Thirty seconds covers a cold antivirus-scanned start on
#: a loaded CI runner; a fifth of a second keeps the pass fast when the sidecar
#: is quick, which it normally is.
DESCRIPTOR_DEADLINE_SECONDS: Final = 30.0
POLL_INTERVAL_SECONDS: Final = 0.2

#: How long the MCP child gets to answer the initialize request. It has to
#: dial the sidecar and complete one probe round-trip first, so this is a
#: link budget, not just a parse budget.
INITIALIZE_DEADLINE_SECONDS: Final = 30.0

#: Shutdown bounds: the polite wait after terminate (or after closing the MCP
#: child's stdin, which is its documented way to exit), then the wait after
#: kill. A child that survives both is reported, never waited on again.
SHUTDOWN_DEADLINE_SECONDS: Final = 10.0
KILL_DEADLINE_SECONDS: Final = 5.0

#: How much of a child's stderr a refusal may quote, and the cap on the one
#: line it becomes. Bounded because a runaway traceback must not become a
#: runaway refusal; whole because the useful sentence is usually the last one.
STDERR_TAIL_BYTES: Final = 2048
DETAIL_CAP: Final = 300

#: The descriptor's published location under the state directory. Mirrors
#: ``pz_agent_core.rpc.descriptor`` (see the module docstring for why this is
#: spelled here rather than imported).
RUNTIME_DIR_NAME: Final = "runtime"
DESCRIPTOR_FILE_NAME: Final = "core-rpc.json"

#: The shipped default configuration, relative to this repository checkout.
EXAMPLE_CONFIG: Final = (
    Path(__file__).resolve().parents[2] / "configs" / "agent" / "config.example.toml"
)

#: The subdirectories Project Zomboid itself creates in its user directory.
#: Created so the scratch directory is shaped like the real thing the
#: ``--zomboid-dir`` flag documents pointing at.
ZOMBOID_CHILDREN: Final = ("Saves", "mods", "Lua", "Logs")

#: The one request this driver speaks, and the id it expects the answer under.
#: The stdio transport is newline-delimited JSON-RPC (one object per line); the
#: protocol version is a released MCP revision, and a server that prefers
#: another one answers with its own in the result rather than refusing — the
#: result still carries ``serverInfo``, which is the observation this driver
#: is for.
INITIALIZE_ID: Final = 1
PROTOCOL_VERSION: Final = "2025-06-18"

#: The single line success prints, asserted verbatim by the unit test.
SUCCESS_PREFIX: Final = "OK: the packaged pair completed an MCP initialize over the RPC link"


class Refusal(Exception):
    """One phase failed. Carries the phase name and what was seen."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"REFUSED: {phase}: {detail}")
        self.phase = phase


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. One place, so ``--help`` is the contract."""
    parser = argparse.ArgumentParser(
        prog="prove_packaged_link",
        description=(
            "start the pz-agent sidecar, then prove the pz-agent-mcp executable completes "
            "an MCP initialize against it over the Core RPC link"
        ),
        epilog=(
            "Each command flag is repeatable, one argv token per occurrence, so a "
            "multi-word command needs no quoting: "
            "--pz-agent=python --pz-agent=-m --pz-agent=pz_agent_cli. The '=' spelling "
            "matters for a token that begins with a dash — argparse otherwise reads "
            "'-m' as an option of this program's own."
        ),
    )
    parser.add_argument(
        "--pz-agent",
        action="append",
        required=True,
        metavar="TOKEN",
        dest="pz_agent",
        help="the sidecar command; repeat the flag to add tokens",
    )
    parser.add_argument(
        "--pz-agent-mcp",
        action="append",
        required=True,
        metavar="TOKEN",
        dest="pz_agent_mcp",
        help="the MCP stdio server command; repeat the flag to add tokens",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        required=True,
        metavar="DIR",
        help="disposable directory for the fake Zomboid profile and the state directory",
    )
    return parser


def _one_line(text: str) -> str:
    """*text* flattened to a single clamped line, so a refusal stays one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= DETAIL_CAP else flat[:DETAIL_CAP] + "…"


def _stderr_tail(path: Path) -> str:
    """The clamped end of a child's stderr file, for a refusal to quote.

    The product keeps the token and the address off this stream, so quoting it
    is safe; the bound is because a traceback's length is not ours to relay.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - STDERR_TAIL_BYTES))
            raw = handle.read(STDERR_TAIL_BYTES)
    except OSError:
        return "<its stderr could not be read>"
    return _one_line(raw.decode("utf-8", errors="replace")) or "<its stderr is empty>"


def _end(child: subprocess.Popen[bytes], name: str) -> None:
    """Stop *child* inside a bound, escalating once, and release its pipes.

    Never raises: this runs on every exit path, and cleanup failing must not
    replace the refusal (or the success) that is already decided.
    """
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=SHUTDOWN_DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=KILL_DEADLINE_SECONDS)
                except subprocess.TimeoutExpired:
                    # Stated rather than waited on again: every wait here has a
                    # deadline, including the last one.
                    sys.stderr.write(f"note: the {name} process outlived kill\n")
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def _prepare_scratch(scratch: Path) -> tuple[Path, Path, Path]:
    """Build the disposable world: a Zomboid-shaped profile, a state dir, a config.

    Returns ``(zomboid_dir, state_dir, config_path)``. The directory names are
    single letters because on POSIX the state directory feeds an ``AF_UNIX``
    socket path with a hard ``sun_path`` length limit; every character spent
    here is one a deep checkout path cannot spend.
    """
    zomboid = scratch / "z"
    state = scratch / "s"
    for child in ZOMBOID_CHILDREN:
        (zomboid / child).mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    # A descriptor left by an earlier run must not satisfy this run's poll:
    # the observation would then be about a file, not about the sidecar.
    (state / RUNTIME_DIR_NAME / DESCRIPTOR_FILE_NAME).unlink(missing_ok=True)
    if not EXAMPLE_CONFIG.is_file():
        raise Refusal(
            "config",
            f"the shipped {EXAMPLE_CONFIG.name} was not found relative to this script; "
            "run the driver from a checkout or a release tree that carries configs/agent/",
        )
    config = state / "config.toml"
    shutil.copyfile(EXAMPLE_CONFIG, config)
    return zomboid, state, config


def _await_descriptor(
    descriptor: Path, sidecar: subprocess.Popen[bytes], stderr_file: Path
) -> None:
    """Poll for the RPC descriptor, bounded, refusing early if the sidecar died."""
    deadline = time.monotonic() + DESCRIPTOR_DEADLINE_SECONDS
    while True:
        if descriptor.is_file():
            return
        code = sidecar.poll()
        if code is not None:
            raise Refusal(
                "sidecar-start",
                f"the sidecar command exited with code {code} before publishing the RPC "
                f"descriptor (stderr: {_stderr_tail(stderr_file)})",
            )
        if time.monotonic() >= deadline:
            raise Refusal(
                "descriptor",
                f"no {DESCRIPTOR_FILE_NAME} appeared under the state directory within "
                f"{DESCRIPTOR_DEADLINE_SECONDS:.0f}s while the sidecar kept running "
                f"(its stderr: {_stderr_tail(stderr_file)})",
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _send_initialize(child: subprocess.Popen[bytes], stderr_file: Path) -> None:
    """Write the one newline-delimited JSON-RPC frame the stdio server accepts."""
    request = {
        "jsonrpc": "2.0",
        "id": INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "prove-packaged-link", "version": "0"},
        },
    }
    assert child.stdin is not None
    try:
        child.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        child.stdin.flush()
    except OSError as gone:
        # A broken pipe here means the MCP process refused before serving —
        # its exit code and stderr say why, so they are what the refusal shows.
        code = child.poll()
        raise Refusal(
            "mcp-start",
            f"the MCP command closed its stdin before serving (exit {code}, "
            f"stderr: {_stderr_tail(stderr_file)})",
        ) from gone


def _observed_server_info(message: dict[str, object]) -> dict[str, object]:
    """The ``serverInfo`` object out of an initialize *result*, or a refusal.

    The validation is the postcondition: an error response, a result without
    ``serverInfo``, or a shape that only resembles one all refuse here, because
    exiting 0 on any of them would report a link that was never proven.
    """
    if "error" in message:
        raise Refusal(
            "initialize",
            f"the server answered a JSON-RPC error, not a result: "
            f"{_one_line(json.dumps(message['error']))}",
        )
    result = message.get("result")
    if not isinstance(result, dict):
        raise Refusal("initialize", "the response carries no result object")
    info = result.get("serverInfo")
    if not isinstance(info, dict) or not info.get("name"):
        raise Refusal("initialize", "the initialize result carries no named serverInfo")
    return info


def _read_initialize_result(child: subprocess.Popen[bytes], stderr_file: Path) -> dict[str, object]:
    """The ``serverInfo`` the server answered, read under a hard deadline.

    A reader thread feeds a queue and the queue is polled with timeouts —
    never a bare blocking read, so a server that accepted the frame and went
    silent fails inside the bound instead of hanging whatever runs this.
    """
    lines: queue.Queue[bytes] = queue.Queue()
    stdout = child.stdout
    assert stdout is not None

    def pump() -> None:
        # EOF ends the loop; a stream closed under the thread during cleanup
        # ends it too, and neither is this thread's news to report.
        with contextlib.suppress(OSError, ValueError):
            for line in stdout:
                lines.put(line)

    reader = threading.Thread(target=pump, name="mcp-stdout-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + INITIALIZE_DEADLINE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal(
                "initialize",
                f"no JSON-RPC answer with id {INITIALIZE_ID} arrived on stdout within "
                f"{INITIALIZE_DEADLINE_SECONDS:.0f}s (MCP stderr: {_stderr_tail(stderr_file)})",
            )
        try:
            line = lines.get(timeout=min(POLL_INTERVAL_SECONDS, remaining))
        except queue.Empty:
            code = child.poll()
            if code is not None and lines.empty():
                # The child is gone; give the reader a bounded moment to drain
                # what was already in the pipe before calling the stream empty.
                reader.join(timeout=1.0)
                if lines.empty():
                    raise Refusal(
                        "mcp-start",
                        f"the MCP command exited with code {code} before answering "
                        f"(stderr: {_stderr_tail(stderr_file)})",
                    ) from None
            continue
        try:
            message: object = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as noise:
            seen = _one_line(line.decode("utf-8", errors="replace"))
            raise Refusal(
                "initialize", f"a non-JSON line arrived on the MCP stdout: {seen!r}"
            ) from noise
        if isinstance(message, dict) and message.get("id") == INITIALIZE_ID:
            return _observed_server_info(message)
        # Anything else on the stream — a notification, someone else's id — is
        # protocol, just not the answer; keep reading under the same deadline.


def _prove(pz_agent: Sequence[str], pz_agent_mcp: Sequence[str], scratch: Path) -> str:
    """Run the whole chain and return the success line, or raise a Refusal."""
    zomboid, state, config = _prepare_scratch(scratch)
    sidecar_argv = [
        *pz_agent,
        "--state-dir",
        str(state),
        "--zomboid-dir",
        str(zomboid),
        "--config",
        str(config),
        "start",
        "--foreground",
    ]
    mcp_argv = [*pz_agent_mcp, "--state-dir", str(state)]
    sidecar_err = scratch / "sidecar.err"
    mcp_err = scratch / "mcp.err"
    sidecar: subprocess.Popen[bytes] | None = None
    mcp: subprocess.Popen[bytes] | None = None
    # Both children's argv are assembled from this driver's own flags, which is
    # what the S603 waivers below state; there is no shell and no interpolation.
    try:
        with (
            (scratch / "sidecar.out").open("wb") as side_out,
            sidecar_err.open("wb") as side_err,
            mcp_err.open("wb") as served_err,
        ):
            sidecar = subprocess.Popen(  # noqa: S603
                sidecar_argv, stdin=subprocess.DEVNULL, stdout=side_out, stderr=side_err
            )
            _await_descriptor(state / RUNTIME_DIR_NAME / DESCRIPTOR_FILE_NAME, sidecar, sidecar_err)
            mcp = subprocess.Popen(  # noqa: S603
                mcp_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=served_err
            )
            _send_initialize(mcp, mcp_err)
            info = _read_initialize_result(mcp, mcp_err)
            # The observation is complete. Shutting the MCP child down by
            # closing its stdin is its documented clean exit; everything after
            # this line is hygiene and cannot change the verdict.
            if mcp.stdin is not None:
                with contextlib.suppress(OSError):
                    mcp.stdin.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                mcp.wait(timeout=SHUTDOWN_DEADLINE_SECONDS)
            return f"{SUCCESS_PREFIX} (serverInfo: {info.get('name')} {info.get('version')})"
    finally:
        if mcp is not None:
            _end(mcp, "pz-agent-mcp")
        if sidecar is not None:
            _end(sidecar, "pz-agent")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the proof and return the exit code. Success only on the observation."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        line = _prove(args.pz_agent, args.pz_agent_mcp, args.scratch.resolve())
    except Refusal as refusal:
        sys.stderr.write(f"{refusal}\n")
        return EXIT_REFUSED
    sys.stdout.write(line + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
