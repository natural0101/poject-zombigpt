"""Console entry point for ``pz-agent-mcp`` (and for ``python -m pz_agent_mcp``).

The module ``pyproject.toml`` points its console script at. It owns three
decisions and no domain logic at all:

* **The SDK is checked before anything else.** ``mcp`` is an optional extra, so
  the common failure on a fresh install is that it is simply not there. That is
  reported as the install step it is — one line naming the extra — and never as
  an ImportError traceback, which tells a user what Python was doing rather than
  what they have to do.
* **Serving needs services, and this process is not the one that owns them.**
  The boundary reads through :class:`~.ports.CoreServices`, which the sidecar
  provides while it holds the exchange directory's lock. An embedder passes them
  in. Nothing in this repository does yet, and the honest answer to ``serve``
  without them is a refusal that names what is missing — the same rule the CLI
  applies by keeping unbacked subcommands out of its parser rather than shipping
  one that parses and does nothing.
* **The catalogue is answerable without either.** ``--describe`` writes the whole
  published surface as JSON. It needs no SDK, no game and no session, which is
  what makes it usable as the thing a client author reads and a docs check
  compares against.

:func:`main` returns an exit code and never calls :func:`sys.exit`, so a test
drives the real entry point in this process and reads exactly what a user would
have seen.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Final, TextIO

from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import PRODUCT_VERSION, PROTOCOL_VERSION

from .catalog import RESOURCES, TOOLS
from .ports import CoreServices
from .server import SERVER_NAME, McpSdkUnavailable, require_sdk, run_stdio

__all__ = [
    "EXIT_NOT_WIRED",
    "EXIT_NO_SDK",
    "EXIT_OK",
    "EXIT_USAGE",
    "NO_SERVICES_MESSAGE",
    "PROGRAM",
    "build_parser",
    "catalogue_document",
    "cli",
    "main",
]

PROGRAM: Final = "pz-agent-mcp"

#: Served the surface, or answered a question about it.
EXIT_OK: Final = 0

#: The command was understood and refused: no core services are attached to
#: this process, so there is nothing to serve.
EXIT_NOT_WIRED: Final = 1

#: A malformed invocation. Matches argparse's own, and the CLI's.
EXIT_USAGE: Final = 2

#: The optional ``mcp`` extra is not installed. Distinct from every other
#: failure because the remedy is a single install command.
EXIT_NO_SDK: Final = 3

#: Said in full rather than as "not implemented": a client author reading this
#: has to know that the missing piece is the sidecar handing its ports over, not
#: something they have configured wrongly.
NO_SERVICES_MESSAGE: Final = (
    "no core services are attached to this process, so there is nothing to serve. "
    "The MCP boundary reads through the ports the sidecar owns while it holds the "
    "exchange directory's lock, and this build has no channel that hands them to a "
    "second process; an embedder must pass them to main(services=...). "
    "Run 'pz-agent-mcp --describe' to read the published surface, and 'pz-agent status' "
    "to see what the sidecar reports."
)


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. One place, so ``--help`` is the contract."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="stdio MCP server for pz-agent",
        epilog=(
            "Configure it in an MCP client as: "
            '{"command": "python", "args": ["-m", "pz_agent_mcp"]}. '
            "The server name it registers under is " + SERVER_NAME + "."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {PRODUCT_VERSION}")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="write the published tools and resources as JSON and exit",
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


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    services: CoreServices | None = None,
) -> int:
    """Run the entry point and return its exit code. Never raises for a user error."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    parser = build_parser()
    try:
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
        require_sdk()
    except McpSdkUnavailable as absent:
        err.write(f"{PROGRAM}: {absent}\n")
        return EXIT_NO_SDK

    if services is None:
        err.write(f"{PROGRAM}: {NO_SERVICES_MESSAGE}\n")
        return EXIT_NOT_WIRED

    asyncio.run(run_stdio(services))
    return EXIT_OK


def cli() -> None:
    """Console-script wrapper: run :func:`main` and exit with its code."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
