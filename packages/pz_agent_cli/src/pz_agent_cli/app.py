"""The ``pz-agent`` argument parser and dispatch.

argparse from the standard library, matching the core package's dependency rule.

**Only commands whose subsystem exists are in the parser.** The blueprint also
names ``start``, ``stop``, ``arm`` and ``disarm``; those drive a running
sidecar, and there is no sidecar process in this build. A subcommand that parsed
and then printed "not implemented" would be a worse answer than an unrecognised
command, because it would look like a runtime failure rather than an honest
absence — argparse's "invalid choice" names exactly what is true. See
``docs/PROGRESS.md`` for what closes them.

:func:`main` returns an exit code and never calls :func:`sys.exit` itself, so a
test drives the real command in-process and reads what a user would have seen.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pz_agent_core.version import PRODUCT_VERSION

from .config import ConfigValidation, load_config
from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, resolve_workspace
from .doctor import run_checks
from .modinstall import (
    ForeignFileError,
    InstallError,
    find_source,
    install_mod,
    uninstall_mod,
)
from .output import Printer
from .saves import run_backup_save, run_restore_save
from .status import run_status
from .support import DEFAULT_LOG_LINES, DEFAULT_REPLAY_LIMIT, run_logs, run_replay

PROGRAM: Final = "pz-agent"

_DESCRIPTION: Final = "Local agent for Project Zomboid Build 42: diagnose, install, operate."

_EPILOG: Final = (
    "Start with 'pz-agent doctor'. Every check it prints has a stable code and "
    "remediation text; docs/TROUBLESHOOTING.md explains the ones whose cause is not "
    "obvious from the message."
)


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. One place, so --help is the contract."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=_DESCRIPTION,
        epilog=_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {PRODUCT_VERSION}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="configuration file (default: <Zomboid>/pz-agent/config.toml)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="where pz-agent keeps logs, traces, backups and bundles",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="game installation, for a copy Steam does not list",
    )
    parser.add_argument(
        "--zomboid-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="the Zomboid user directory, when the game runs with -cachedir",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = subparsers.add_parser(
        "doctor", help="check the installation and the capability surface"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable, redacted report")

    install = subparsers.add_parser(
        "install-mod", help="copy the bridge mod into the Zomboid mods directory"
    )
    install.add_argument(
        "--source", type=Path, default=None, metavar="PATH", help="mod source directory"
    )
    install.add_argument("--json", action="store_true")

    uninstall = subparsers.add_parser(
        "uninstall-mod", help="remove exactly the files install-mod wrote"
    )
    uninstall.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="what the exchange directory reports now")
    status.add_argument("--json", action="store_true")

    backup = subparsers.add_parser("backup-save", help="hash-manifested copy of a save")
    backup.add_argument("save_id", nargs="?", default=None, help="<mode>/<save name>")
    backup.add_argument("--list", action="store_true", dest="list_only", help="list backups")
    backup.add_argument(
        "--prune", type=int, default=None, metavar="KEEP", help="delete all but the newest KEEP"
    )
    backup.add_argument("--json", action="store_true")

    restore = subparsers.add_parser(
        "restore-save", help="restore a verified backup; refuses while the game is open"
    )
    restore.add_argument("backup_id")
    restore.add_argument("--json", action="store_true")

    logs = subparsers.add_parser("logs", help="recent diagnostics, or a support bundle")
    logs.add_argument("--lines", type=int, default=DEFAULT_LOG_LINES)
    logs.add_argument("--json", action="store_true")
    logs.add_argument("--bundle", action="store_true", help="build a redacted support archive")
    logs.add_argument(
        "--verify", action="store_true", help="print what the archive contains after redaction"
    )
    logs.add_argument("--output", type=Path, default=None, metavar="PATH")

    replay = subparsers.add_parser("replay", help="step through a recorded trace")
    replay.add_argument("trace", type=Path)
    replay.add_argument("--limit", type=int, default=DEFAULT_REPLAY_LIMIT)
    replay.add_argument("--json", action="store_true")

    validate = subparsers.add_parser("validate-config", help="validate config.toml before start")
    validate.add_argument("--json", action="store_true")

    return parser


def run_validate_config(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent validate-config``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    validation = load_config(workspace.config_path)
    if as_json:
        payload = validation.to_dict()
        payload["path"] = workspace.redact(workspace.config_path)
        printer.json(payload)
        return EXIT_OK if validation.ok else EXIT_FAILURE
    _render_validation(
        validation, workspace_path=workspace.redact(workspace.config_path), printer=printer
    )
    return EXIT_OK if validation.ok else EXIT_FAILURE


def _render_validation(
    validation: ConfigValidation, *, workspace_path: str, printer: Printer
) -> None:
    printer.heading(f"validate-config {workspace_path}")
    for problem in validation.errors:
        printer.error(problem.render())
    for problem in validation.warnings:
        printer.line(f"warning: {problem.render()}")
    if validation.ok:
        printer.line("configuration is valid")
    else:
        printer.line(f"{len(validation.errors)} error(s); the agent will not start")


def run_install_mod(ctx: CliContext, *, source: Path | None, as_json: bool) -> int:
    """Handler for ``pz-agent install-mod``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    mods_dir = workspace.mods_dir
    if mods_dir is None:
        printer.error(
            "no Zomboid directory was found, so there is no mods folder to install into. "
            "Run pz-agent doctor and read PZD003."
        )
        return EXIT_FAILURE
    chosen = source or ctx.mod_source_override or find_source()
    if chosen is None:
        printer.error(
            "the mod source was not found next to this installation. Pass --source with "
            "the path to pz-mod/42 from a checkout of this repository."
        )
        return EXIT_FAILURE
    try:
        result = install_mod(chosen, mods_dir, clock=ctx.now)
    except ForeignFileError as exc:
        printer.error(
            f"refusing to install: {workspace.redactor.path(exc.path)} — {exc.reason}. "
            "Nothing was written. Move or delete that file, or run uninstall-mod first."
        )
        return EXIT_FAILURE
    except InstallError as exc:
        printer.error(f"install failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    if as_json:
        printer.json(
            {
                "destination": workspace.redact(result.destination),
                "files_written": result.files_written,
                "bytes_written": result.bytes_written,
                "replaced": list(result.replaced),
                "removed_stale": list(result.removed_stale),
                "manifest": result.manifest.to_dict(),
            }
        )
        return EXIT_OK
    printer.line(
        f"installed {result.files_written} file(s) into {workspace.redact(result.destination)}"
    )
    printer.field("bytes", str(result.bytes_written))
    if result.replaced:
        printer.field("replaced", f"{len(result.replaced)} previously installed file(s)")
    if result.removed_stale:
        printer.field("removed", f"{len(result.removed_stale)} file(s) from an older version")
    printer.line("")
    printer.line("Enable 'PZ Agent Bridge' in the game's Mods menu, then reload the save —")
    printer.line("enabling a mod does not affect a game that is already loaded.")
    return EXIT_OK


def run_uninstall_mod(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent uninstall-mod``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    mods_dir = workspace.mods_dir
    if mods_dir is None:
        printer.error("no Zomboid directory was found, so nothing can be uninstalled")
        return EXIT_FAILURE
    try:
        result = uninstall_mod(mods_dir)
    except InstallError as exc:
        printer.error(f"uninstall failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    if as_json:
        printer.json(
            {
                "destination": workspace.redact(result.destination),
                "removed": list(result.removed),
                "kept_modified": list(result.kept_modified),
                "missing": list(result.missing),
                "directory_removed": result.directory_removed,
            }
        )
        return EXIT_OK
    printer.line(
        f"removed {len(result.removed)} file(s) from {workspace.redact(result.destination)}"
    )
    if result.kept_modified:
        printer.line("kept, because they were modified after install:")
        printer.lines(f"  {path}" for path in result.kept_modified)
    if result.missing:
        printer.field("already gone", f"{len(result.missing)} file(s)")
    if not result.directory_removed:
        printer.field("note", "the mod directory still holds files pz-agent did not install")
    printer.line("Saves, backups and configuration were not touched.")
    return EXIT_OK


def run_doctor(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent doctor``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    report = run_checks(ctx, workspace)
    if as_json:
        printer.json(report.to_dict())
    else:
        printer.lines(report.render_lines())
    return EXIT_OK if report.ok else EXIT_FAILURE


def dispatch(ctx: CliContext, args: argparse.Namespace) -> int:
    """Route a parsed invocation to its handler."""
    command = args.command
    if command == "doctor":
        return run_doctor(ctx, as_json=args.json)
    if command == "install-mod":
        return run_install_mod(ctx, source=args.source, as_json=args.json)
    if command == "uninstall-mod":
        return run_uninstall_mod(ctx, as_json=args.json)
    if command == "status":
        return run_status(ctx, as_json=args.json)
    if command == "backup-save":
        return run_backup_save(
            ctx,
            save_id=args.save_id,
            list_only=args.list_only,
            prune=args.prune,
            as_json=args.json,
        )
    if command == "restore-save":
        return run_restore_save(ctx, backup_id=args.backup_id, as_json=args.json)
    if command == "logs":
        return run_logs(
            ctx,
            lines=args.lines,
            as_json=args.json,
            bundle=args.bundle,
            verify=args.verify,
            output=args.output,
        )
    if command == "replay":
        return run_replay(ctx, trace=args.trace, as_json=args.json, limit=args.limit)
    if command == "validate-config":
        return run_validate_config(ctx, as_json=args.json)
    # Unreachable through the parser: every choice it accepts is handled above,
    # and an unknown one is rejected before dispatch.
    raise AssertionError(f"unrouted command: {command!r}")


def main(argv: Sequence[str] | None = None, context: CliContext | None = None) -> int:
    """Entry point. Returns an exit code; never raises for a user error."""
    ctx = context or CliContext.from_process()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        parser.print_help(ctx.stdout)
        return EXIT_USAGE
    resolved = ctx.with_overrides(
        state_dir=args.state_dir,
        config=args.config,
        install_dir=args.install_dir,
        zomboid_dir=args.zomboid_dir,
    )
    return dispatch(resolved, args)
