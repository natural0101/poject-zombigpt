"""``pz-agent latency`` — the P0 latency targets, measured rather than asserted.

The epic that set the targets (submit → accepted ≤ 250 ms p95, terminal ack
visible ≤ 250 ms, observations at 4 Hz, safety stop ≤ 200 ms) shipped no
instrument, so this command is it: it reads the exchange directory's journals
through :func:`pz_agent_core.diagnostics.collect_latency` and prints what was
actually recorded — distributions with exact nearest-rank percentiles, one row
per interval, and nothing invented for the intervals the directory does not
record.

The exit-code rule is deliberate and narrow. UNMEASURED exits 0 even under
``--targets``, because the machine that has no live data — every CI runner,
every machine the game has never run on — measures nothing, and a check that
fails there would be failing on the absence of a game rather than on a missed
target. Only a target that was *measured and missed* fails the command, and
only when ``--targets`` asked for the comparison; without the flag this is a
report, and a report that ran is a report that succeeded.
"""

from __future__ import annotations

from pz_agent_core.diagnostics import (
    MAX_TRACES,
    Distribution,
    LatencyError,
    LatencyReport,
    TargetVerdict,
    collect_latency,
    evaluate_targets,
)

from .context import EXIT_FAILURE, EXIT_OK, CliContext, resolve_workspace
from .output import Printer


def _table_rows(report: LatencyReport) -> tuple[tuple[str, Distribution], ...]:
    """The human table's rows, in the order a command travels."""
    return (
        ("submit -> accepted", report.submit_to_accepted),
        ("accepted -> started", report.accepted_to_started),
        ("started -> terminal", report.started_to_terminal),
        ("submit -> terminal", report.submit_to_terminal),
        ("safety.stop submit -> terminal", report.safety_submit_to_terminal),
        ("observation interval", report.observation_intervals),
        ("game heartbeat interval", report.game_heartbeat_intervals),
        ("sidecar heartbeat interval", report.sidecar_heartbeat_intervals),
    )


def _cell(value: int | None) -> str:
    return "-" if value is None else str(value)


def _render_table(report: LatencyReport, printer: Printer) -> None:
    """One row per interval, with a clocks column so the cross-clock rows are
    visibly not the same kind of number as the single-clock ones."""
    printer.line(
        f"  {'interval (ms)':<32} {'count':>6} {'min':>7} {'p50':>7} {'p95':>7} {'max':>7}  clocks"
    )
    for label, distribution in _table_rows(report):
        clocks = "cross" if distribution.cross_clock else "same"
        if not distribution.measured:
            printer.line(f"  {label:<32} {'unmeasured':>6}")
            continue
        printer.line(
            f"  {label:<32} {distribution.count:>6} {_cell(distribution.minimum):>7} "
            f"{_cell(distribution.p50):>7} {_cell(distribution.p95):>7} "
            f"{_cell(distribution.maximum):>7}  {clocks}"
        )


def _render_report(report: LatencyReport, printer: Printer, *, root: str) -> None:
    printer.heading("pz-agent latency")
    printer.field("exchange directory", root or "(none)")
    printer.field("commands traced", f"{len(report.traces)} (cap: newest {MAX_TRACES})")
    printer.field("pending", f"{report.pending} command(s) with no terminal ack")
    if report.dropped_commands:
        printer.field("dropped", f"{report.dropped_commands} oldest command(s) beyond the cap")
    if report.unmatched_acks:
        printer.field("unmatched acks", str(report.unmatched_acks))
    hz = report.implied_observation_hz
    printer.field(
        "observation cadence",
        f"~{hz} Hz implied by the median interval" if hz is not None else "unmeasured",
    )
    for fact in report.heartbeats:
        printer.field(f"{fact.peer} heartbeat", f"seq {fact.seq} at {fact.timestamp_ms} ms")
    printer.line()
    _render_table(report, printer)
    printer.line()
    printer.line("Cross-clock rows subtract the sidecar's clock from the game's; no skew is")
    printer.line("corrected anywhere, so those numbers include it and may be negative.")
    if report.diagnostics:
        printer.line()
        printer.field("diagnostics", str(len(report.diagnostics)))
        printer.lines(f"    {detail}" for detail in report.diagnostics)


def run_latency(ctx: CliContext, *, as_json: bool, targets: bool) -> int:
    """Handler for ``pz-agent latency``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    root = workspace.ipc_root
    try:
        # No Zomboid directory means no exchange directory has ever existed:
        # the same honest answer as an empty one — everything is unmeasured.
        report = LatencyReport.empty() if root is None else collect_latency(root)
    except LatencyError as exc:
        printer.error(f"latency could not be measured: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    checks = evaluate_targets(report) if targets else ()
    if as_json:
        document = report.to_dict()
        document["ipc_root"] = workspace.redact(root)
        if targets:
            document["targets"] = [check.to_dict() for check in checks]
        printer.json(document)
    else:
        _render_report(report, printer, root=workspace.redact(root))
        if targets:
            printer.line()
            printer.heading("P0 targets")
            for check in checks:
                printer.field(check.verdict.value.upper(), check.name, width=10)
                printer.line(f"    {check.description}")
                printer.line(f"    {check.detail}")
    missed = any(check.verdict is TargetVerdict.MISSED for check in checks)
    return EXIT_FAILURE if missed else EXIT_OK
