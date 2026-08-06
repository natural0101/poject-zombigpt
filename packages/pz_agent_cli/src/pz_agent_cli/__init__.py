"""The ``pz-agent`` command-line interface.

This is the only package permitted to write to stdout — everything else
communicates through structured results and the diagnostics logger.

The commands are argparse subcommands built in :mod:`pz_agent_cli.app`. Each one
is a function taking a :class:`~pz_agent_cli.context.CliContext` and returning an
exit code, so the tests drive the real command in-process against a temporary
tree rather than inspecting a subprocess's output.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

from .app import COMMANDS, PROGRAM, build_parser, dispatch, main
from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace, resolve_workspace

__all__ = [
    "COMMANDS",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "PRODUCT_VERSION",
    "PROGRAM",
    "CliContext",
    "Workspace",
    "build_parser",
    "dispatch",
    "main",
    "resolve_workspace",
]
