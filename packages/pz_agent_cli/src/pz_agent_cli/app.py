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

``voice`` is the same shape of command pointed at the same exchange directory
from a second process: ``voice run`` drives the companion in
:mod:`pz_agent_voice` over the ports in :mod:`pz_agent_cli.voice`, and ``voice
check`` answers what a phrase resolves to with no game, no session and no
microphone — which is how a user finds out why «стоп» was not recognised.

:func:`main` returns an exit code and never calls :func:`sys.exit` itself, so a
test drives the real command in-process and reads what a user would have seen.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.diagnostics import DiagnosticLog, LogLevel, TraceWriter
from pz_agent_core.goals import GoalQueue
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import JsonDict, SessionMode
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_mcp.server import SERVER_NAME as MCP_SERVER_NAME

from .autonomy import (
    PLANNER_FILE_NAME,
    AutonomyPlanner,
    PlannerRecord,
    build_backup_witness,
    build_planner,
    publish_planner_record,
)
from .config import AgentConfig, ConfigValidation, default_config, load_config
from .context import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    TRACE_NAME,
    CliContext,
    Workspace,
    resolve_workspace,
)
from .core_services import serve_core_rpc
from .doctor import check_capabilities, run_checks
from .latency import run_latency
from .livetest import add_live_test_parser, run_live_test
from .memory import SidecarMemory, add_remember_parser, build_sidecar_memory, run_remember
from .modinstall import (
    ForeignFileError,
    InstallError,
    find_source,
    install_mod,
    uninstall_mod,
)
from .navigation_planner import NavigatingPlanner, unwrap_planner
from .output import Printer
from .runtime import (
    CAPABILITY_FILE_NAME,
    DEFAULT_LIMITS,
    CapabilityLedger,
    LoopLimits,
    RunSummary,
    ShutdownOutcome,
    SidecarLoop,
)
from .saves import run_backup_save, run_restore_save
from .smoke import default_scenario_dir, run_smoke
from .status import game_liveness, run_status
from .supervisor import GameRunningProbe, SidecarSupervisor, SupervisorState, probe_game_running
from .support import _LOG_STEM, DEFAULT_LOG_LINES, DEFAULT_REPLAY_LIMIT, run_logs, run_replay
from .voice import add_voice_parser, run_voice

PROGRAM: Final = "pz-agent"

#: The key an MCP client's configuration files the server under. Taken from the
#: server rather than spelled again here, so the block ``start`` prints and the
#: three files in ``configs/mcp/`` cannot come to disagree about the name.
SERVER_KEY: Final = MCP_SERVER_NAME

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
    "remember",
    "voice",
    "logs",
    "replay",
    "latency",
    "validate-config",
    "smoke",
    "live-test",
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

    _add_save_parsers(subparsers)
    add_remember_parser(subparsers)
    add_voice_parser(subparsers)

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

    latency = subparsers.add_parser(
        "latency", help="measure the latency targets from the exchange directory's journals"
    )
    latency.add_argument("--json", action="store_true", help="the raw report document")
    latency.add_argument(
        "--targets",
        action="store_true",
        help="compare against the P0 targets; exits non-zero only on a measured miss",
    )

    validate = subparsers.add_parser("validate-config", help="validate config.toml before start")
    validate.add_argument("--json", action="store_true")

    _add_smoke_parser(subparsers)
    add_live_test_parser(subparsers)

    return parser


def _add_save_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The two save commands, split out to keep ``build_parser`` readable."""
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


def build_capabilities(workspace: Workspace) -> CapabilityLedger:
    """Resolve this machine's capability surface for a sidecar session.

    ``doctor``'s check is reused rather than re-implemented: it already scans the
    installed Lua read-only, resolves every declared probe against it and writes
    the report, and a second scanner here would be a second opinion about the
    same install that nothing compares.

    A scan that cannot run — no installation found, ``media/lua`` unreadable —
    produces a ledger with no report, which answers False to every capability.
    That is the only honest direction: allowing everything because the scan
    failed is precisely the assumption
    :func:`~pz_agent_core.actions.engine.deny_capability` exists to prevent. The
    reason is carried on the ledger and published to the state directory, so
    ``pz-agent status`` says capabilities were not resolved *and why* rather than
    the sidecar refusing every command with no explanation anywhere.
    """
    check, report, notes = check_capabilities(workspace)
    redact = workspace.redactor.text
    if report is None:
        detail = redact(f"the capability scan could not run: {check.detail}. {check.remediation}")
    else:
        detail = redact(f"{check.code}: {check.detail}")
    install = workspace.install_dir
    return CapabilityLedger(
        record_path=workspace.state_dir / CAPABILITY_FILE_NAME,
        report=report,
        detail=detail,
        report_path=workspace.capability_report_path,
        # The one user decision that reaches the ledger. Read here rather than
        # threaded through, because this is where the ledger is built and the
        # alternative is a second place that has to remember to apply it.
        disabled=_disabled_capabilities(workspace),
        protected_roots=(install,) if install is not None else (),
        notes=tuple(redact(note) for note in notes),
    )


def build_loop(
    ctx: CliContext,
    workspace: Workspace,
    *,
    limits: LoopLimits,
    trace: TraceWriter | None = None,
) -> SidecarLoop:
    """Assemble the loop from the parts that already exist.

    The registry is built here rather than shared, because registration is
    single-assignment: a module-level singleton reused by two sessions would
    raise on the second one.

    The capability ledger is built here for the opposite reason: it is the one
    thing the loop cannot default to safely. Every game adapter names a
    ``required_capability``, and the engine's fail-closed default refuses all of
    them, so a sidecar assembled without a ledger attaches, arms and then
    declines every action a user asks for.

    The planner is the same shape of defect one layer up. ``SidecarLoop._act``
    returns immediately when there is none, so a sidecar assembled without one
    observes, guards, and never proposes anything — ``arm --mode autonomous``
    grants an authority nothing exercises. It is assembled here and its record
    is written from the loop that was actually built, so ``status`` reports what
    is running rather than what this function meant to build.

    The memory is the same defect one layer deeper again, and quieter than
    either: a planner whose memory is the empty one answers "nothing is
    reserved" to every question, so §7.9 rests on nothing and the user has no
    way to protect an item at all. It is built here, handed to the planner, and
    its record is read back off the loop for the same reason the planner's is.
    """
    ipc_root = workspace.ipc_root
    if ipc_root is None:
        raise InstallError("no Zomboid directory was found, so there is no exchange directory")
    registry = register_game_adapters(register_builtins(AdapterRegistry()))
    capabilities = build_capabilities(workspace)
    capabilities.publish()
    memory = build_sidecar_memory(workspace)
    planner, record = _build_planner(
        ctx, workspace, registry=registry, capabilities=capabilities, memory=memory
    )
    # The navigating wrapper is always on: navigate_to goals are walked by the
    # deterministic route executor whatever planner (or none) sits behind it,
    # and every other kind passes through to the wrapped planner untouched.
    navigating = NavigatingPlanner(planner)
    loop = SidecarLoop(
        layout=IpcLayout(ipc_root),
        state_dir=workspace.state_dir,
        registry=registry,
        clock=ctx.clock_ms,
        limits=limits,
        pid_file=build_supervisor(ctx, workspace).pid_file,
        capabilities=capabilities,
        planner=navigating,
        trace=trace,
        # The typed goal channel, on the loop's own clock and born disarmed —
        # SidecarLoop.arm is the only thing that arms it, the same rule the
        # loop applies to itself. Built beside the AutonomyPlanner because that
        # planner serves goals (GoalPlanner.propose_for_goal); a queue beside a
        # planner that could not would admit goals nothing ever activates.
        goals=GoalQueue(clock=ctx.clock_ms, armed=False),
    )
    # Bound after construction because the loop takes its planner as a
    # constructor argument: the wrapper needs the loop's queue, lock and
    # action channel, and until this call it serves navigation to nobody.
    navigating.bind(loop)
    publish_planner_record(
        workspace.state_dir / PLANNER_FILE_NAME,
        # Read off the loop, not off the intent above: this is the field that
        # would have caught the planner never reaching the constructor.
        replace(record, wired=loop.planner is not None),
    )
    # The same reading for memory, one level further in: it is not enough that a
    # planner reached the loop, it has to be the planner holding *this* memory.
    memory.wired = _planner_memory(loop) is memory
    memory.publish()
    return loop


def _disabled_capabilities(workspace: Workspace) -> frozenset[str]:
    """What ``safety.disabled_capabilities`` switches off, or nothing.

    A configuration that does not validate switches nothing off, deliberately.
    The alternative — reading the raw table anyway — would let a document with a
    typo elsewhere in it decide what the agent may do, and a user who cannot see
    their own file's errors is exactly the one who should not be silently
    obeyed. ``validate-config`` and ``doctor`` report the document; this
    function does not act on one it could not trust.
    """
    validated = load_config(workspace.config_path).config
    if validated is None:
        return frozenset()
    names = validated.get("safety", "disabled_capabilities")
    return frozenset(str(name) for name in names) if isinstance(names, list) else frozenset()


def _planner_memory(loop: SidecarLoop) -> object | None:
    """What the assembled loop's planner will actually ask about reservations.

    Read off the loop rather than off the local that was passed in, so a memory
    that stopped short of the planner is a fact ``status`` reports rather than
    an assembly that only looks right.
    """
    # The navigating wrapper serves navigate_to itself and delegates every
    # other kind; the memory-holding planner is the one it wraps.
    planner = unwrap_planner(loop.planner)
    return planner.memory if isinstance(planner, AutonomyPlanner) else None


def _build_planner(
    ctx: CliContext,
    workspace: Workspace,
    *,
    registry: AdapterRegistry,
    capabilities: CapabilityLedger,
    memory: SidecarMemory,
) -> tuple[AutonomyPlanner, PlannerRecord]:
    """The planner for this workspace, and what to tell ``status`` about it.

    The configuration is read again rather than passed in, because ``build_loop``
    is reached from a detached sidecar as well as from ``run_start``. A document
    that does not validate does not stop the assembly: the loop still has to come
    up so the user can be told what is wrong, and the documented defaults —
    ``provider = "none"``, the deterministic path — are what runs meanwhile.

    The backup witness is built here rather than defaulted for the same reason
    the capability ledger is: the fail-closed default
    (:func:`~pz_agent_cli.autonomy.no_attributable_backup`) makes an armed
    autonomous sidecar ask about every need, and which of the two a machine gets
    is a fact about its backups, not about this function. The note that comes
    back with it says which one happened, so ``status`` can report it.

    The memory is passed in rather than built here because the caller has to
    keep hold of it: it publishes its own record, and only the caller can see
    whether it reached the loop.
    """
    validation = load_config(workspace.config_path)
    config: AgentConfig = validation.config or default_config()
    backup, backup_note = build_backup_witness(workspace)
    notes = [backup_note]
    if validation.config is None:
        notes.append(
            f"the configuration did not validate ({len(validation.errors)} problem(s)), so the "
            "documented defaults are in force; run 'pz-agent validate-config' to see them"
        )
    planner, record = build_planner(
        registry=registry,
        capabilities=capabilities,
        config=config,
        env=ctx.env,
        clock=ctx.clock_ms,
        backup=backup,
        memory=memory,
        notes=tuple(workspace.redactor.text(note) for note in notes),
    )
    # A fallback reason is a provider's own message about an endpoint or a
    # credential, and it is printed by ``status`` and carried in its ``--json``
    # — which is the form that ends up in a bug report.
    return planner, replace(record, fallback_reason=workspace.redactor.text(record.fallback_reason))


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
    loop = build_loop(ctx, workspace, limits=limits, trace=_sidecar_trace(ctx, workspace))
    log = _sidecar_log(ctx, workspace)
    attach = loop.attach()
    if not attach.attached:
        _log_safely(log, LogLevel.ERROR, "attach.refused", detail=attach.detail)
        printer.error(workspace.redactor.text(attach.detail))
        return EXIT_FAILURE
    _log_safely(
        log,
        LogLevel.INFO,
        "attach.ok",
        detail=attach.detail,
        resumed=attach.resumed,
        session_id=None if attach.session is None else attach.session.session_id,
    )
    supervisor = build_supervisor(ctx, workspace)
    supervisor.pid_file.claim(os.getpid())
    # The Core RPC link, served after a successful attach and before the loop
    # runs: this process owns the session, so this process is the server half.
    # A sidecar that cannot publish the link still observes and guards — the
    # failure is stated rather than fatal, because trading the reflex guard for
    # a diagnostic endpoint would protect the wrong thing.
    try:
        endpoint = serve_core_rpc(
            supervisor,
            loop,
            doctor=lambda: run_checks(ctx, workspace),
            log_file=workspace.logs_dir / f"{_LOG_STEM}.jsonl",
        )
    except Exception as exc:
        # Any failure to bind, publish or key the endpoint lands here, and all
        # of them trade the same way: the link is lost, the session is not.
        _log_safely(
            log,
            LogLevel.ERROR,
            "rpc.refused",
            detail=f"{type(exc).__name__}: {exc}",
        )
        printer.error(
            "the Core RPC link could not be served "
            f"({workspace.redactor.text(str(exc))}); the sidecar runs on, but "
            "pz-agent-mcp and voice will find nothing to connect to"
        )
    else:
        _log_safely(
            log,
            LogLevel.INFO,
            "rpc.serving",
            descriptor=str(endpoint.descriptor_file.name),
            family=endpoint.descriptor.family,
        )
    try:
        summary = loop.run()
    finally:
        shutdown = loop.shutdown(reason="the foreground loop ended")
        # After the loop, before anything else: the descriptor must go before
        # a client can dial a core that no longer ticks. `stop_rpc` is safe
        # whether or not `serve_core_rpc` succeeded.
        supervisor.stop_rpc()
        _log_run(log, loop, summary=locals().get("summary"), shutdown=shutdown)
    # Named only when it exists: the writer creates the directory at startup and
    # the file on the first observation, so a run with no game behind it leaves
    # nothing to replay, and pointing a user at an absent path is worse than
    # saying nothing.
    trace = loop.trace.path if loop.trace is not None and loop.trace.path.is_file() else None
    if as_json:
        printer.json(
            {
                "attached": attach.detail,
                "ticks": summary.ticks,
                "cause": summary.cause.value,
                "detail": summary.detail,
                "lock_released": shutdown.lock_released,
                "mode": loop.mode.value,
                "trace": workspace.redact(trace),
            }
        )
        return EXIT_OK
    printer.line(workspace.redactor.text(attach.detail))
    printer.field("ticks", str(summary.ticks))
    printer.field("stopped", summary.detail)
    printer.field("lock", "released" if shutdown.lock_released else "was not held")
    if trace is not None:
        printer.field("trace", f"pz-agent replay {workspace.redact(trace)}")
    return EXIT_OK


def _sidecar_log(ctx: CliContext, workspace: Workspace) -> DiagnosticLog | None:
    """The sidecar's own log, or None when the directory will not take one.

    ``DiagnosticLog`` was written, tested, rotated and redacted — and
    constructed nowhere outside the test suite. Nothing wrote ``pz-agent.log``
    or ``pz-agent.jsonl``, while nineteen of the twenty live scenarios name the
    first among the logs to collect, ``docs/LOCAL_DEBUG_MAP.md`` sends an
    operator to it, ``pz-agent logs`` reads it and the support bundle packs the
    directory it lives in. Every one of those was reading a file that could not
    exist.

    Returning None rather than raising: a sidecar that will not start because
    its log directory is read-only has traded the whole product for its
    diagnostics.
    """
    try:
        workspace.logs_dir.mkdir(parents=True, exist_ok=True)
        return DiagnosticLog(workspace.logs_dir, redactor=workspace.redactor, clock=ctx.clock_ms)
    except OSError:
        return None


def _sidecar_trace(ctx: CliContext, workspace: Workspace) -> TraceWriter | None:
    """The file ``pz-agent replay`` reads, or None when it cannot be opened.

    ``TraceWriter`` had the same defect ``DiagnosticLog`` did and one more
    document resting on it: ``docs/QUICKSTART.md`` tells a user to run
    ``pz-agent replay <trace>`` when something goes wrong, the support bundle
    packs ``traces/*.jsonl``, and nothing in the product had ever written a
    trace — so the remedy named a file that could not exist.

    One rotating file per workspace rather than one per run: an operator running
    ``replay`` needs a path they can predict, and the writer's own bound is what
    keeps a long session from filling a disk.
    """
    try:
        workspace.trace_dir.mkdir(parents=True, exist_ok=True)
        return TraceWriter(
            workspace.trace_dir / TRACE_NAME, redactor=workspace.redactor, clock=ctx.clock_ms
        )
    except OSError:
        return None


def _log_safely(log: DiagnosticLog | None, level: LogLevel, event: str, **fields: Any) -> None:
    """Write one record, and never let logging be why the sidecar stopped.

    A full disk, a file locked by a virus scanner mid-run, a directory removed
    underneath a long session: all of them are reasons to lose a log line and
    none is a reason to end a session that is driving a character.
    """
    if log is None:
        return
    try:
        log.log(level, event, **fields)
    except OSError:
        return


def _log_run(
    log: DiagnosticLog | None,
    loop: SidecarLoop,
    *,
    summary: object,
    shutdown: ShutdownOutcome,
) -> None:
    """Everything the run retained, written once at the end.

    At the end rather than per tick, deliberately. The loop keeps its
    diagnostics and its safety events in bounded rings already, so writing them
    here costs one pass over two small deques and puts nothing on the path that
    drives the character. Per-action tracing is the obvious next step and is
    *not* done here: it belongs in the tick, and adding writes to the tick is
    not a change to make immediately before a two-and-a-half-hour live run
    nobody can rehearse.
    """
    if log is None:
        return
    if isinstance(summary, RunSummary):
        _log_safely(
            log,
            LogLevel.INFO,
            "run.finished",
            ticks=summary.ticks,
            cause=summary.cause.value,
            detail=summary.detail,
        )
    for event in loop.recent_events:
        _log_safely(
            log,
            LogLevel.WARNING,
            "safety.event",
            reason_code=event.reason_code.value,
            priority=event.priority.name,
            message=event.message,
            forces_disarm=event.forces_disarm,
        )
    _log_safely(
        log,
        LogLevel.INFO,
        "shutdown",
        lock_released=shutdown.lock_released,
        detail=shutdown.detail,
        lost=len(shutdown.lost),
    )


def mcp_client_entry(workspace: Workspace, *, redacted: bool) -> JsonDict:
    """The stdio server configuration §14.3 asks ``start`` to print, as a document.

    A **dictionary**, serialised by :func:`json.dumps`, never assembled from
    f-strings. The previous version pasted the interpreter path into a quoted
    string by hand, so on Windows it produced

        "command": "C:\\Users\\Иван\\...\\python.exe"

    with single backslashes — which is not JSON. ``json.loads`` refuses it with
    *Invalid \\escape*, and the block ``pz-agent start`` prints for a user to
    paste into their client was unparseable on the operating system this
    release is for. Building the document and letting the encoder escape it is
    not a style preference; it is the only way the output is correct for paths
    the author did not think of.

    ``--state-dir`` is passed explicitly rather than left to discovery. The
    server has to find the same workspace this sidecar is using, and a client
    launches it as a detached process with its own environment — so the one
    thing that reliably carries the answer is an argument. This replaces the
    ``PZ_AGENT_STATE_DIR`` environment variable that used to be printed here and
    that nothing read; ``env`` stays empty, as the three shipped configurations
    and ``configs/mcp/README.md`` have always said.
    """
    interpreter = workspace.redactor.text(sys.executable) if redacted else sys.executable
    state_dir = workspace.redact(workspace.state_dir) if redacted else str(workspace.state_dir)
    return {
        SERVER_KEY: {
            "command": interpreter,
            "args": ["-m", "pz_agent_mcp", "--state-dir", state_dir],
            "env": {},
        }
    }


def _mcp_snippet(workspace: Workspace, *, redacted: bool) -> tuple[str, ...]:
    """:func:`mcp_client_entry` rendered as the lines ``start`` prints."""
    document = mcp_client_entry(workspace, redacted=redacted)
    rendered = json.dumps(document, ensure_ascii=False, indent=2)
    # The braces of the outer object are dropped: what is printed is the entry a
    # user pastes *inside* their client's "mcpServers" object, which is how
    # every client documents it and how configs/mcp/*.json are shaped.
    return tuple(
        line[2:] if line.startswith("  ") else line for line in rendered.splitlines()[1:-1]
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
    if command == "remember":
        return run_remember(ctx, args)
    if command == "voice":
        return run_voice(ctx, args)
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
    if command == "latency":
        return run_latency(ctx, as_json=args.json, targets=args.targets)
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
    if command == "live-test":
        return run_live_test(ctx, args)
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
