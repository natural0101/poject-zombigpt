"""The ``pz-agent`` argument parser and dispatch.

argparse from the standard library, matching the core package's dependency rule.

**Only commands whose subsystem exists are in the parser.** Every choice here is
backed by code that does the thing: a subcommand that parsed and then printed
"not implemented" would be a worse answer than an unrecognised command, because
it would look like a runtime failure rather than an honest absence.

``start``, ``stop``, ``arm`` and ``disarm`` drive the loop in
:mod:`pz_agent_cli.runtime` through the process lifecycle in
:mod:`pz_agent_cli.supervisor`. ``start`` attaches in ``OBSERVE`` and stays
there: arming is a separate command, on purpose, and no flag on ``start``
changes that.

:func:`main` returns an exit code and never calls :func:`sys.exit` itself, so a
test drives the real command in-process and reads what a user would have seen.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import SessionMode
from pz_agent_core.version import PRODUCT_VERSION

from .config import ConfigValidation, load_config
from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace, resolve_workspace
from .doctor import run_checks
from .modinstall import (
    ForeignFileError,
    InstallError,
    find_source,
    install_mod,
    uninstall_mod,
)
from .output import Printer
from .runtime import DEFAULT_LIMITS, LoopLimits, SidecarLoop
from .saves import run_backup_save, run_restore_save
from .smoke import default_scenario_dir, run_smoke
from .status import game_liveness, run_status
from .supervisor import GameRunningProbe, SidecarSupervisor, SupervisorState, probe_game_running
from .support import DEFAULT_LOG_LINES, DEFAULT_REPLAY_LIMIT, run_logs, run_replay

PROGRAM: Final = "pz-agent"

#: The commands this build wires. Every one of them has a subsystem behind it.
COMMANDS: Final[tuple[str, ...]] = (
    "doctor",
    "install-mod",
    "uninstall-mod",
    "status",
    "start",
    "stop",
    "arm",
    "disarm",
    "backup-save",
    "restore-save",
    "logs",
    "replay",
    "validate-config",
)

#: Modes ``pz-agent arm`` accepts on the command line, lowercased for typing.
ARM_MODES: Final[tuple[str, ...]] = ("assisted", "autonomous")

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

    start = subparsers.add_parser("start", help="run the sidecar; it attaches in OBSERVE")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="run the loop in this terminal instead of detaching it",
    )
    start.add_argument(
        "--ticks",
        type=int,
        default=None,
        metavar="N",
        help="stop a foreground loop after N ticks; the default is the whole budget",
    )
    start.add_argument("--json", action="store_true")

    stop = subparsers.add_parser("stop", help="ask a running sidecar to shut down")
    stop.add_argument("--json", action="store_true")

    arm = subparsers.add_parser("arm", help="grant a running sidecar authority to act")
    arm.add_argument(
        "--mode",
        choices=ARM_MODES,
        default=ARM_MODES[0],
        help="how much authority to grant (default: assisted)",
    )
    arm.add_argument("--json", action="store_true")

    disarm = subparsers.add_parser("disarm", help="return a running sidecar to OBSERVE")
    disarm.add_argument("--json", action="store_true")

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

    _add_smoke_parser(subparsers)

    return parser


def _add_smoke_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The ``smoke`` subcommand, split out to keep ``build_parser`` readable."""
    smoke = subparsers.add_parser(
        "smoke",
        help="validate the game-smoke scenarios and report what a live run would ask of you",
    )
    smoke.add_argument(
        "--scenario-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="scenario directory (default: tests/game-smoke in a source checkout)",
    )
    smoke.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="write one evidence file per scenario here, including the unrun ones",
    )
    smoke.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="ID",
        help="run only this scenario; repeatable. Everything else is still reported as not run",
    )
    # A live run drives a session this process does not own, so --dry-run is
    # the only mode that does anything here. It is not the default: asking for
    # a live run and being told plainly why it cannot happen is better than
    # silently downgrading to a validation pass the caller did not request.
    smoke.add_argument("--dry-run", action="store_true", help="validate without touching a game")
    smoke.add_argument("--json", action="store_true")


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
    except OSError as exc:
        # Reading the source or hashing the destination can fail on a locked or
        # unreadable file. That is a user's environment, not a defect, and it
        # reports as a message with an exit code rather than a traceback.
        printer.error(
            f"install failed while reading the filesystem: "
            f"{workspace.redactor.text(exc.strerror or str(exc))}"
        )
        return EXIT_FAILURE
    if as_json:
        printer.json(
            {
                "destination": workspace.redact(result.destination),
                "files_written": result.files_written,
                "bytes_written": result.bytes_written,
                "replaced": list(result.replaced),
                "removed_stale": list(result.removed_stale),
                "kept_stale": list(result.kept_stale),
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
    if result.kept_stale:
        printer.field("kept", "an older version's file(s) you edited, not deleted:")
        printer.lines(f"    {path}" for path in result.kept_stale)
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
    except OSError as exc:
        printer.error(
            f"uninstall stopped while reading the filesystem: "
            f"{workspace.redactor.text(exc.strerror or str(exc))}. Nothing further was "
            "removed; the manifest still records what was installed."
        )
        return EXIT_FAILURE
    exchange = "" if workspace.ipc_root is None else workspace.redact(workspace.ipc_root)
    if as_json:
        printer.json(
            {
                "destination": workspace.redact(result.destination),
                "removed": list(result.removed),
                "kept_modified": list(result.kept_modified),
                "missing": list(result.missing),
                "directory_removed": result.directory_removed,
                "exchange_directory_left": exchange,
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
    if exchange:
        # Stated rather than deleted: the exchange directory is written by the
        # mod and the sidecar, not by install-mod, and this command removes only
        # what it put there. Removing files it never wrote is how an uninstaller
        # takes something else with it.
        printer.field("left in place", f"{exchange} (written by the mod, not by install-mod)")
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


def build_supervisor(ctx: CliContext, workspace: Workspace) -> SidecarSupervisor:
    """The process-lifecycle handle for this workspace's state directory."""
    return SidecarSupervisor(workspace.state_dir, clock=ctx.clock_ms)


def build_loop(ctx: CliContext, workspace: Workspace, *, limits: LoopLimits) -> SidecarLoop:
    """Assemble the loop from the parts that already exist.

    The registry is built here rather than shared, because registration is
    single-assignment: a module-level singleton reused by two sessions would
    raise on the second one.
    """
    ipc_root = workspace.ipc_root
    if ipc_root is None:
        raise InstallError("no Zomboid directory was found, so there is no exchange directory")
    registry = register_game_adapters(register_builtins(AdapterRegistry()))
    return SidecarLoop(
        layout=IpcLayout(ipc_root),
        state_dir=workspace.state_dir,
        registry=registry,
        clock=ctx.clock_ms,
        limits=limits,
        pid_file=build_supervisor(ctx, workspace).pid_file,
    )


def _sidecar_argv(workspace: Workspace) -> list[str]:
    """The command line a detached sidecar is started with.

    Composed from resolved paths rather than from the user's original argv, so
    the child does not have to repeat this machine's discovery — and so nothing
    a user typed is passed through to a process spawn unexamined.
    """
    argv = [sys.executable, "-m", "pz_agent_cli"]
    argv += ["--state-dir", str(workspace.state_dir)]
    argv += ["--config", str(workspace.config_path)]
    user_dir = workspace.user_dir
    if user_dir is not None:
        argv += ["--zomboid-dir", str(user_dir)]
    argv += ["start", "--foreground"]
    return argv


def run_start(ctx: CliContext, *, foreground: bool, ticks: int | None, as_json: bool) -> int:
    """Handler for ``pz-agent start`` (blueprint §14.3).

    Validates the configuration first, because a sidecar that starts and then
    refuses every command on a bad setting has spent the user's attention to
    tell them something the validator could have said immediately.
    """
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    validation = load_config(workspace.config_path)
    if not validation.ok:
        _render_validation(
            validation, workspace_path=workspace.redact(workspace.config_path), printer=printer
        )
        printer.error("the sidecar was not started")
        return EXIT_FAILURE
    if workspace.ipc_root is None:
        printer.error(
            "no Zomboid directory was found, so there is no exchange directory to attach "
            "to. Run pz-agent doctor and read PZD003."
        )
        return EXIT_FAILURE
    supervisor = build_supervisor(ctx, workspace)
    if foreground:
        return _start_foreground(ctx, workspace, ticks=ticks, as_json=as_json, printer=printer)
    outcome = supervisor.start(_sidecar_argv(workspace))
    game = probe_game(ctx, workspace)
    if as_json:
        printer.json(
            {
                "started": outcome.started,
                "detail": workspace.redactor.text(outcome.detail),
                "mode": SessionMode.OBSERVE.value,
                "record": None if outcome.record is None else outcome.record.to_dict(),
                "game": game.to_dict(),
                "mcp": _mcp_snippet(workspace, redacted=True),
            }
        )
        return EXIT_OK if outcome.started else EXIT_FAILURE
    if not outcome.started:
        printer.error(outcome.detail)
        return EXIT_FAILURE
    printer.line(outcome.detail)
    printer.field("mode", "OBSERVE — it will not act until you run 'pz-agent arm'")
    printer.field("game", game.detail)
    printer.field("logs", workspace.redact(supervisor.spawn_log))
    printer.line("")
    printer.line("MCP stdio server, for a client that speaks it:")
    printer.lines(f"  {line}" for line in _mcp_snippet(workspace, redacted=False))
    return EXIT_OK


def probe_game(ctx: CliContext, workspace: Workspace) -> GameRunningProbe:
    """Whether Project Zomboid is open, from the heartbeat first and the process table second.

    The same three-valued answer ``restore-save`` needs, reported here because
    ``start`` is where a user finds out the sidecar has nothing to attach to yet.
    """
    return probe_game_running(heartbeat=game_liveness(ctx, workspace))


def _start_foreground(
    ctx: CliContext,
    workspace: Workspace,
    *,
    ticks: int | None,
    as_json: bool,
    printer: Printer,
) -> int:
    limits = DEFAULT_LIMITS if ticks is None else LoopLimits(tick_budget=ticks)
    loop = build_loop(ctx, workspace, limits=limits)
    attach = loop.attach()
    if not attach.attached:
        printer.error(workspace.redactor.text(attach.detail))
        return EXIT_FAILURE
    supervisor = build_supervisor(ctx, workspace)
    supervisor.pid_file.claim(os.getpid())
    try:
        summary = loop.run()
    finally:
        shutdown = loop.shutdown(reason="the foreground loop ended")
    if as_json:
        printer.json(
            {
                "attached": attach.detail,
                "ticks": summary.ticks,
                "cause": summary.cause.value,
                "detail": summary.detail,
                "lock_released": shutdown.lock_released,
                "mode": loop.mode.value,
            }
        )
        return EXIT_OK
    printer.line(workspace.redactor.text(attach.detail))
    printer.field("ticks", str(summary.ticks))
    printer.field("stopped", summary.detail)
    printer.field("lock", "released" if shutdown.lock_released else "was not held")
    return EXIT_OK


def _mcp_snippet(workspace: Workspace, *, redacted: bool) -> tuple[str, ...]:
    """The stdio server configuration §14.3 asks ``start`` to print.

    Printed with real paths so it can be pasted into a client's configuration,
    and redacted in the ``--json`` form, which is the one that ends up in a bug
    report — an interpreter path carries the account name.
    """
    interpreter = workspace.redactor.text(sys.executable) if redacted else sys.executable
    state_dir = workspace.redact(workspace.state_dir) if redacted else str(workspace.state_dir)
    return (
        '"pz-agent": {',
        f'  "command": "{interpreter}",',
        '  "args": ["-m", "pz_agent_mcp"],',
        f'  "env": {{"PZ_AGENT_STATE_DIR": "{state_dir}"}}',
        "}",
    )


def run_stop(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent stop``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    outcome = build_supervisor(ctx, workspace).request_stop()
    detail = workspace.redactor.text(outcome.detail)
    if as_json:
        printer.json(
            {
                "requested": outcome.requested,
                "signalled": outcome.signalled,
                "detail": detail,
                "record": None if outcome.record is None else outcome.record.to_dict(),
            }
        )
        return EXIT_OK if outcome.requested else EXIT_FAILURE
    if not outcome.requested:
        printer.error(detail)
        return EXIT_FAILURE
    printer.line(detail)
    return EXIT_OK


def run_arm(ctx: CliContext, *, mode: str, as_json: bool) -> int:
    """Handler for ``pz-agent arm``.

    Publishes a request; it does not arm anything itself. The loop applies it,
    and refuses it if the game is silent, if the panic sentinel is present, or if
    the guard demanded a disarm on the same tick — none of which this process can
    see, and none of which it should be second-guessing from outside.
    """
    return _control(ctx, arm_mode=SessionMode(mode.upper()), as_json=as_json)


def run_disarm(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent disarm``."""
    return _control(ctx, arm_mode=None, as_json=as_json)


def _control(ctx: CliContext, *, arm_mode: SessionMode | None, as_json: bool) -> int:
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    supervisor = build_supervisor(ctx, workspace)
    status = supervisor.status()
    if status.state is not SupervisorState.RUNNING:
        message = (
            f"no sidecar is running to receive this: {workspace.redactor.text(status.detail)}. "
            "Start one with 'pz-agent start'."
        )
        if as_json:
            printer.json({"delivered": False, "detail": message, "state": status.state.value})
        else:
            printer.error(message)
        return EXIT_FAILURE
    request = supervisor.disarm() if arm_mode is None else supervisor.arm(arm_mode)
    verb = "disarm" if arm_mode is None else f"arm in {arm_mode.value}"
    detail = (
        f"asked the sidecar (pid {status.record.pid}) to {verb}; "
        "run 'pz-agent status' to see whether it did"
        if status.record is not None
        else f"asked the running sidecar to {verb}"
    )
    if as_json:
        printer.json({"delivered": True, "detail": detail, "request": request.to_dict()})
    else:
        printer.line(detail)
    return EXIT_OK


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
    if command == "start":
        return run_start(ctx, foreground=args.foreground, ticks=args.ticks, as_json=args.json)
    if command == "stop":
        return run_stop(ctx, as_json=args.json)
    if command == "arm":
        return run_arm(ctx, mode=args.mode, as_json=args.json)
    if command == "disarm":
        return run_disarm(ctx, as_json=args.json)
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
    if command == "smoke":
        return run_smoke(
            scenario_dir=args.scenario_dir or default_scenario_dir(),
            evidence_dir=args.evidence_dir,
            only=args.scenario,
            dry_run=args.dry_run,
            timestamp_ms=ctx.clock_ms(),
            emit=Printer(ctx.stdout, ctx.stderr).line,
            as_json=args.json,
        )
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
