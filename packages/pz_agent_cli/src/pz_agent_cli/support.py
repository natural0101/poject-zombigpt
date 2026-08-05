"""``pz-agent logs`` and ``pz-agent replay``.

``logs`` shows the recent human-readable log, or builds the support bundle
described in blueprint §14.7. ``--verify`` prints exactly what the finished
archive contains after redaction, which is the check
``docs/TROUBLESHOOTING.md`` asks a user to run before attaching it to a public
issue — so verification happens here, before the path is printed, rather than
being something the user is told to do afterwards.

``replay`` walks a trace file: every entry in order, and the observation series
rebuilt from the recorded snapshots and diffs. A trace that cannot be replayed
faithfully says so; it does not print a partial world.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pz_agent_core.diagnostics import (
    BundleBuilder,
    BundleError,
    SupportBundle,
    TraceError,
    TraceKind,
    TraceRead,
    iso_from_ms,
    read_records,
    read_trace,
    replay_observations,
    verify_bundle,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION

from .config import load_config
from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace, resolve_workspace
from .doctor import environment_facts, run_checks
from .output import Printer

#: Log lines shown by ``logs`` when no count is given.
DEFAULT_LOG_LINES: Final = 50

MAX_LOG_LINES: Final = 2_000

#: Bytes of each log and journal tail copied into a bundle.
BUNDLE_TAIL_BYTES: Final = 256 * 1024

#: Trace entries ``replay`` prints before it stops and says how many remain.
DEFAULT_REPLAY_LIMIT: Final = 200

_LOG_STEM: Final = "pz-agent"


def _tail(path: Path, lines: int, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, ...]:
    """The last *lines* lines of a text file, reading a bounded window."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ()
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    return tuple(raw.decode("utf-8", errors="replace").splitlines()[-lines:])


def run_logs(
    ctx: CliContext,
    *,
    lines: int,
    as_json: bool,
    bundle: bool,
    verify: bool,
    output: Path | None,
) -> int:
    """Handler for ``pz-agent logs``."""
    printer = Printer(ctx.stdout, ctx.stderr)
    if verify and not bundle:
        printer.error("--verify describes a bundle; pass --bundle as well")
        return EXIT_USAGE
    if lines < 1 or lines > MAX_LOG_LINES:
        printer.error(f"--lines must be between 1 and {MAX_LOG_LINES}")
        return EXIT_USAGE
    workspace = resolve_workspace(ctx)
    if bundle:
        return _run_bundle(ctx, workspace, printer, verify=verify, output=output, as_json=as_json)
    return _show_logs(workspace, printer, lines=lines, as_json=as_json)


def _show_logs(workspace: Workspace, printer: Printer, *, lines: int, as_json: bool) -> int:
    if as_json:
        records, problems = read_records(workspace.logs_dir / f"{_LOG_STEM}.jsonl", limit=lines)
        printer.json({"records": list(records), "problems": list(problems)})
        return EXIT_OK
    text_log = workspace.logs_dir / f"{_LOG_STEM}.log"
    tail = _tail(text_log, lines)
    if not tail:
        printer.line(f"no log yet at {workspace.redact(text_log)}")
        return EXIT_OK
    printer.lines(tail)
    return EXIT_OK


def build_support_bundle(ctx: CliContext, workspace: Workspace, destination: Path) -> SupportBundle:
    """Assemble the bundle §14.7 describes.

    Every member goes in through the builder, which redacts it. Nothing from the
    save directory and nothing from the game installation is added — the closest
    it comes is the capability report, which records symbol names and file
    hashes and never a line of game source.

    Raises:
        BundleError: when a member breaches a bound or the archive cannot be
            written.
    """
    builder = BundleBuilder(redactor=workspace.redactor, clock=ctx.now)
    builder.add_json(
        "versions.json",
        {
            "product_version": PRODUCT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "mod_version": MOD_VERSION,
            "environment": environment_facts(workspace),
        },
    )
    report = run_checks(ctx, workspace)
    builder.add_json("doctor.json", report.to_dict())
    if report.capabilities is not None:
        builder.add_json("capabilities.json", report.capabilities.to_dict())
    _add_config(builder, workspace)
    _add_directory(builder, "logs", workspace.logs_dir, ("*.log", "*.jsonl"))
    _add_directory(builder, "traces", workspace.trace_dir, ("*.jsonl",))
    _add_protocol_trace(builder, workspace)
    return builder.build(destination)


def _add_config(builder: BundleBuilder, workspace: Workspace) -> None:
    validation = load_config(workspace.config_path)
    if validation.config is not None:
        builder.add_text("config.toml", validation.config.to_toml())
    builder.add_json("config-validation.json", validation.to_dict())


def _add_directory(
    builder: BundleBuilder, prefix: str, directory: Path, patterns: Sequence[str]
) -> None:
    """Add every matching file in *directory*, bounded and non-recursive."""
    if not directory.is_dir():
        return
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            try:
                builder.add_file(f"{prefix}/{path.name}", path, tail_bytes=BUNDLE_TAIL_BYTES)
            except BundleError:
                # A bundle that is missing one log is still worth having; one
                # that fails to build because a file was locked is not.
                builder.add_text(
                    f"{prefix}/{path.name}.unreadable.txt",
                    f"{path.name} could not be read when the bundle was built\n",
                )


def _add_protocol_trace(builder: BundleBuilder, workspace: Workspace) -> None:
    """Add the tails of the three IPC journals — the wire trace of §14.7."""
    if workspace.ipc_root is None or not workspace.ipc_root.is_dir():
        return
    layout = IpcLayout(workspace.ipc_root)
    for journal in layout.journals():
        if journal.is_file():
            builder.add_file(f"ipc/{journal.name}", journal, tail_bytes=BUNDLE_TAIL_BYTES)


def _bundle_name(ctx: CliContext) -> str:
    stamp = ctx.now().strftime("%Y%m%dT%H%M%SZ")
    return f"pz-agent-support-{stamp}.zip"


def _run_bundle(
    ctx: CliContext,
    workspace: Workspace,
    printer: Printer,
    *,
    verify: bool,
    output: Path | None,
    as_json: bool,
) -> int:
    destination = output or (workspace.bundle_dir / _bundle_name(ctx))
    try:
        bundle = build_support_bundle(ctx, workspace, destination)
    except BundleError as exc:
        printer.error(f"support bundle failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    shown = workspace.redact(destination)
    if not verify:
        if as_json:
            payload = bundle.to_dict()
            payload["path"] = shown
            printer.json(payload)
        else:
            printer.line(f"wrote {shown}")
            printer.line("run with --verify before attaching it to a public issue")
        return EXIT_OK

    user_dir = workspace.user_dir
    forbidden = [str(user_dir), str(user_dir.parent), user_dir.parent.name] if user_dir else []
    verification = verify_bundle(destination, redactor=workspace.redactor, forbidden=forbidden)
    if as_json:
        payload = bundle.to_dict()
        payload["path"] = shown
        printer.json({"bundle": payload, "verification": verification.to_dict()})
    else:
        printer.lines(verification.render_lines())
        printer.line("")
        printer.line(f"wrote {shown}")
    return EXIT_OK if verification.clean else EXIT_FAILURE


def run_replay(ctx: CliContext, *, trace: Path, as_json: bool, limit: int) -> int:
    """Handler for ``pz-agent replay``."""
    printer = Printer(ctx.stdout, ctx.stderr)
    if limit < 1:
        printer.error("--limit must be positive")
        return EXIT_USAGE
    try:
        read = read_trace(trace)
    except TraceError as exc:
        printer.error(str(exc))
        return EXIT_FAILURE

    rebuilt = 0
    replay_problem = ""
    try:
        rebuilt = len(replay_observations(read.entries))
    except TraceError as exc:
        replay_problem = str(exc)

    if as_json:
        printer.json(_replay_json(read, limit=limit, rebuilt=rebuilt, problem=replay_problem))
        return EXIT_FAILURE if replay_problem else EXIT_OK

    printer.heading(f"replay {trace.name}")
    for entry in read.entries[:limit]:
        printer.line(f"  #{entry.seq:<5} {iso_from_ms(entry.timestamp_ms)}  {entry.summary()}")
    if len(read.entries) > limit:
        printer.line(f"  ... {len(read.entries) - limit} further entry(ies) not shown")
    printer.line("")
    actions = len(read.of_kind(TraceKind.ACTION))
    printer.field("entries", str(len(read.entries)))
    printer.field("actions", str(actions))
    printer.field("observations rebuilt", str(rebuilt))
    if read.truncated:
        printer.field("truncated", "the trace was longer than the read bound")
    printer.lines(f"  ! {problem}" for problem in read.problems)
    if replay_problem:
        printer.error(f"the observation series could not be replayed: {replay_problem}")
        return EXIT_FAILURE
    return EXIT_OK


def _replay_json(read: TraceRead, *, limit: int, rebuilt: int, problem: str) -> JsonDict:
    return {
        "entries": [entry.to_dict() for entry in read.entries[:limit]],
        "entry_count": len(read.entries),
        "problems": list(read.problems),
        "truncated": read.truncated,
        "observations_rebuilt": rebuilt,
        "replay_problem": problem,
    }
