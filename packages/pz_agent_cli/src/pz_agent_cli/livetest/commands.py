"""``pz-agent live-test`` — six subcommands over the evidence tree.

Each one does a real thing and refuses honestly when it cannot:

* ``prepare``  builds the tree, and will not proceed unless a *test* save exists
  and a backup of it verifies. It never touches a save itself.
* ``run``      runs one scenario, or every one still pending.
* ``status``   the table: every scenario, its state, when it last ran.
* ``resume``   continues from the first scenario that is not PASS.
* ``collect``  copies logs, journals and snapshots into the scenario folders.
* ``finalize`` builds the manifest, or names everything that is missing.

``run`` has no way to drive the game by itself — that is the sidecar's job, and
this process does not own the session. So a run without observations records a
``BLOCKED`` attempt naming what it lacked, rather than a pass it cannot support.
The operator drives the scenario in-game with the sidecar attached, writes down
what was read back, and hands that file to ``--observations``. The runner then
decides, and the observations format has no field for a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pz_agent_core.platform.backup import BackupError, BackupManager
from pz_agent_core.protocol import JsonDict

from ..context import (
    EXIT_FAILURE,
    EXIT_OK,
    TRACE_NAME,
    CliContext,
    Workspace,
    resolve_workspace,
)
from ..output import Printer
from .evidence import (
    COLLECTED_NAME,
    EvidenceLayout,
    LiveTestError,
    collect_files,
    write_document,
)
from .runner import (
    FileDriver,
    FinalizeRefused,
    ScenarioDriver,
    UnavailableDriver,
    attempt_lines,
    default_evidence_root,
    default_manifest_path,
    first_unpassed,
    read_commit,
    repo_root,
    run_scenario,
    summarise,
)
from .runner import (
    finalize as build_manifest,
)
from .scenarios import SCENARIO_IDS, LiveScenario, UnknownScenarioError, by_id, resolve
from .state import LiveState, StateStore

SUBCOMMANDS: Final[tuple[str, ...]] = (
    "prepare",
    "run",
    "status",
    "resume",
    "collect",
    "finalize",
)

#: A save this harness will operate against has to say so in its name. The
#: check is crude on purpose: the alternative is a flag that means "yes, this
#: really is the throwaway world", which is exactly the flag somebody passes at
#: two in the morning to get past a refusal.
TEST_SAVE_MARKER: Final = re.compile(r"test", re.IGNORECASE)

#: Reason recorded on an attempt that never reached a game.
_NO_OBSERVATIONS: Final = (
    "no observations were supplied, so nothing was seen. Drive the scenario in-game "
    "with the sidecar attached and pass the values you read back with --observations."
)

#: Exchange-directory and log files ``collect`` looks for, by the names the
#: scenarios declare in their ``logs`` lists.
_JOURNAL_NAMES: Final = (
    "command.queue.0001.jsonl",
    "command.ack.0001.jsonl",
    "observation.events.0001.jsonl",
)
_SNAPSHOT_NAMES: Final = (
    "observation.snapshot.a.json",
    "observation.snapshot.b.json",
    "observation.snapshot.pointer",
    "session.json",
    "capabilities.json",
)
_HEARTBEAT_NAMES: Final = ("heartbeat.game.json", "heartbeat.sidecar.json")
_SIDECAR_LOG_NAMES: Final = ("pz-agent.log", "pz-agent.jsonl")
_GAME_CONSOLE: Final = "console.txt"


def add_live_test_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``live-test`` group and its six subcommands."""
    group = subparsers.add_parser(
        "live-test",
        help=f"run the {len(SCENARIO_IDS)} live scenarios and build the evidence they produce",
        description=(
            "The live-test harness. Scenarios start at NOT_RUN and only a run that "
            "observed every declared postcondition can move one to PASS."
        ),
    )
    group.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="evidence tree (default: evidence/ in a source checkout)",
    )
    group.add_argument("--json", action="store_true", help="machine-readable output")
    inner = group.add_subparsers(dest="live_command", metavar="SUBCOMMAND")

    prepare = inner.add_parser(
        "prepare", help="build the evidence tree; require a backed-up test save"
    )
    prepare.add_argument(
        "--save",
        default=None,
        metavar="MODE/NAME",
        help="the test save the scenarios will run against",
    )

    run = inner.add_parser("run", help="run one scenario, or every scenario still pending")
    run.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="ID",
        help="scenario id; repeatable. Omit to run every scenario that is not PASS",
    )
    run.add_argument(
        "--observations",
        type=Path,
        default=None,
        metavar="PATH",
        help="observed values for one scenario, read back from the running session",
    )

    status = inner.add_parser("status", help="every scenario, its state and when it last ran")
    status.add_argument(
        "--verbose", action="store_true", help="list every attempt, not only the verdict"
    )

    resume = inner.add_parser("resume", help="continue from the first scenario that is not PASS")
    resume.add_argument(
        "--observations",
        type=Path,
        default=None,
        metavar="PATH",
        help="observed values for the scenario resume starts from",
    )

    collect = inner.add_parser(
        "collect", help="copy logs, journals and snapshots into a scenario folder"
    )
    collect.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="ID",
        help="scenario id; repeatable. Omit to collect for every scenario that has run",
    )

    finalize = inner.add_parser(
        "finalize", help="build the evidence manifest, or name everything that is missing"
    )
    finalize.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="manifest path (default: release/evidence-manifest.json)",
    )


def run_live_test(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch a parsed ``live-test`` invocation."""
    printer = Printer(ctx.stdout, ctx.stderr)
    subcommand = getattr(args, "live_command", None)
    if subcommand is None:
        printer.error(f"live-test needs a subcommand: {', '.join(SUBCOMMANDS)}")
        return EXIT_FAILURE
    layout = EvidenceLayout(args.evidence_dir or default_evidence_root())
    store = StateStore(layout.root)
    as_json = bool(args.json)

    try:
        if subcommand == "prepare":
            return _prepare(ctx, layout, store, printer, save=args.save, as_json=as_json)
        if subcommand == "run":
            return _run(
                layout,
                store,
                printer,
                only=args.scenario,
                observations=args.observations,
                as_json=as_json,
            )
        if subcommand == "status":
            return _status(layout, store, printer, verbose=args.verbose, as_json=as_json)
        if subcommand == "resume":
            return _resume(layout, store, printer, observations=args.observations, as_json=as_json)
        if subcommand == "collect":
            return _collect(ctx, layout, store, printer, only=args.scenario, as_json=as_json)
        if subcommand == "finalize":
            return _finalize(layout, store, printer, output=args.output, as_json=as_json)
    except UnknownScenarioError as exc:
        printer.error(str(exc))
        return EXIT_FAILURE
    except LiveTestError as exc:
        printer.error(f"live-test: {exc}")
        return EXIT_FAILURE
    # Unreachable through the parser; every choice it accepts is handled above.
    raise AssertionError(f"unrouted live-test subcommand: {subcommand!r}")


def _prepare(
    ctx: CliContext,
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    save: str | None,
    as_json: bool,
) -> int:
    """Build the tree and prove the world is safe to experiment on.

    The two refusals here are the point of the subcommand. A save whose name
    does not mark it as a test world is refused because these scenarios
    deliberately hurt the character, start fires of a sort, and end in restores.
    A save with no *verified* backup is refused because "a backup directory
    exists" and "a backup that reads back" are different claims, and only the
    second one survives a restore.
    """
    workspace = resolve_workspace(ctx)
    created = layout.ensure_tree(SCENARIO_IDS)
    initialised = store.initialise(SCENARIO_IDS)

    save_id, backup, problems = _verify_test_save(workspace, save)
    for schema in (layout.result_schema, layout.manifest_schema):
        if not schema.is_file():
            # Without the schemas nothing can be written at all, so this is a
            # prepare-time refusal rather than a surprise on the first run.
            #
            # The remedy is spelled out because the failure has a shape that
            # looks like a bug: the schemas live in the evidence tree, which the
            # release archive ships and a checkout has in git, so an operator
            # only meets this by pointing --evidence-dir somewhere new — or by
            # invoking the bundled executable directly, where "the directory I
            # came from" is a temporary unpack folder. Every other refusal in
            # this project names its way out; this one did not.
            problems.append(
                f"evidence schema missing: {schema}. The two schemas ship in the release "
                "archive's evidence/schema/ and are in git in a checkout. Point "
                "--evidence-dir at that tree (run-live-tests.bat does this for you), or "
                "copy evidence/schema/*.json into the directory you chose."
            )

    document: JsonDict = {
        "evidence_root": str(layout.root),
        "scenario_count": len(SCENARIO_IDS),
        "directories_created": len(created),
        "ledgers_initialised": list(initialised),
        "save_id": save_id or "",
        "backup_id": "" if backup is None else backup,
        "problems": problems,
        "ready": not problems,
    }
    if not problems:
        write_document(layout.prepare_path, document, schema=None)

    if as_json:
        printer.json(document)
        return EXIT_OK if not problems else EXIT_FAILURE
    printer.heading(f"live-test prepare {layout.root}")
    printer.field("scenarios", f"{len(SCENARIO_IDS)} ledgers, {len(initialised)} newly created")
    printer.field("test save", save_id or "not established")
    printer.field("verified backup", backup or "none")
    for problem in problems:
        printer.error(problem)
    if problems:
        printer.error("prepare did not complete; nothing was written to the prepare record")
        return EXIT_FAILURE
    printer.line("ready. Every scenario is NOT_RUN until a run observes its postconditions.")
    return EXIT_OK


def _verify_test_save(
    workspace: Workspace, save: str | None
) -> tuple[str | None, str | None, list[str]]:
    """Resolve the test save and the backup that covers it.

    Returns ``(save_id, backup_id, problems)``. Nothing here writes, restores or
    prunes: prepare's whole relationship with the save directory is read-only,
    which is how "refuse to touch the main save" is guaranteed rather than
    promised.
    """
    problems: list[str] = []
    user_dir = workspace.user_dir
    if user_dir is None:
        return (
            None,
            None,
            ["no Zomboid directory was found, so no save could be checked. Run pz-agent doctor."],
        )
    if save is None:
        return (
            None,
            None,
            [
                "pass --save <mode>/<name> naming the dedicated test world. There is no "
                "default: guessing which save to experiment on is how a main save gets used."
            ],
        )
    if not TEST_SAVE_MARKER.search(save):
        return (
            save,
            None,
            [
                f"refusing to prepare against {save!r}: the save name does not contain 'test'. "
                "These scenarios wound the character, interrupt actions and end in a restore. "
                "Create a dedicated test world and name it so."
            ],
        )

    manager = BackupManager(user_dir, workspace.backup_root)
    if not (manager.saves_dir / Path(save)).is_dir():
        problems.append(
            f"no save directory at {save!r} under {workspace.redact(manager.saves_dir)}"
        )
        return save, None, problems

    candidates = [record for record in manager.list_backups() if record.save_id == save]
    if not candidates:
        return (
            save,
            None,
            [
                f"no backup of {save!r} exists. Run 'pz-agent backup-save {save}' before arming "
                "anything: every scenario after S01 changes the world."
            ],
        )
    # list_backups is newest first, and the newest is the one that matters: an
    # older backup verifying says nothing about the state the world is in now.
    newest = candidates[0]
    try:
        manager.verify(newest.backup_id)
    except BackupError as exc:
        return (
            save,
            None,
            [
                f"the newest backup of {save!r} ({newest.backup_id}) does not verify: {exc}. "
                "A backup that does not read back is not a backup; take another one."
            ],
        )
    return save, newest.backup_id, problems


def _driver_for(observations: Path | None) -> ScenarioDriver:
    if observations is None:
        return UnavailableDriver(reason=_NO_OBSERVATIONS)
    return FileDriver(path=observations)


def _unprepared(layout: EvidenceLayout) -> str | None:
    """Why these scenarios may not be driven yet, or None when they may.

    ``prepare`` is the subcommand that proves the world is safe to experiment
    on: a save whose name marks it as a test world, and a backup that *reads
    back* rather than merely existing. It wrote ``prepare.json`` when both held
    — and nothing read it. ``run`` proceeded regardless, so the one check
    standing between a batch of deliberately destructive scenarios and somebody's
    main save was a check whose answer nobody consulted.

    Read here rather than trusted from memory of an earlier invocation: the
    record is in the evidence tree, which is what a resumed run picks up.
    """
    record = layout.prepare_path
    if not record.is_file():
        return (
            "prepare has not completed for this evidence tree. These scenarios hurt the "
            "character and end in restores, so they do not run until a test save and a "
            "verified backup exist. Run: pz-agent live-test prepare --save <mode>/<name>"
        )
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the prepare record at {record} could not be read ({exc}); run prepare again"
    if not isinstance(document, dict) or document.get("ready") is not True:
        return f"the prepare record at {record} does not say the tree is ready; run prepare again"
    return None


def _run(
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    only: Sequence[str] | None,
    observations: Path | None,
    as_json: bool,
) -> int:
    refusal = _unprepared(layout)
    if refusal is not None:
        printer.error(refusal)
        return EXIT_FAILURE
    selected = _selection(store, only)
    if not selected:
        # Reachable only with ``only`` unset: ``resolve`` refuses a selection
        # that names nothing, so an explicit request can no longer arrive here
        # and be answered with a sentence about every scenario passing. It used
        # to — ``--scenario ""`` printed this line and exited 0 with all of them
        # NOT_RUN — which is why the condition is spelled out rather than left
        # as "empty means done".
        printer.line(f"nothing to run: all {len(SCENARIO_IDS)} scenarios are PASS.")
        return EXIT_OK
    if observations is not None and len(selected) != 1:
        printer.error(
            f"--observations describes one scenario, but {len(selected)} were selected. "
            "Name the scenario with --scenario."
        )
        return EXIT_FAILURE
    return _drive(layout, store, printer, selected, observations=observations, as_json=as_json)


def _resume(
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    observations: Path | None,
    as_json: bool,
) -> int:
    """Run from the first scenario that is not PASS, stopping at the first that still is not.

    Stopping is the useful behaviour: the scenarios build on one another, so
    carrying on past a failure produces a column of failures whose first entry
    is the only one worth reading.
    """
    refusal = _unprepared(layout)
    if refusal is not None:
        printer.error(refusal)
        return EXIT_FAILURE
    start = first_unpassed(store)
    if start is None:
        printer.line(f"nothing to resume: all {len(SCENARIO_IDS)} scenarios are PASS.")
        return EXIT_OK
    remaining = SCENARIO_IDS[SCENARIO_IDS.index(start) :]
    pending = tuple(by_id(sid) for sid in remaining if store.read(sid).state is not LiveState.PASS)
    if observations is not None:
        pending = pending[:1]
    return _drive(layout, store, printer, pending, observations=observations, as_json=as_json)


def _drive(
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    scenarios: Sequence[LiveScenario],
    *,
    observations: Path | None,
    as_json: bool,
) -> int:
    commit = read_commit(repo_root())
    driver = _driver_for(observations)
    records: list[JsonDict] = []
    worst = EXIT_OK
    for scenario in scenarios:
        run = run_scenario(scenario, layout=layout, store=store, driver=driver, commit=commit)
        records.append(
            {
                "scenario_id": run.scenario_id,
                "attempt": run.attempt,
                "status": run.status.value,
                "state": run.state.state.value,
                "failure_code": run.failure_code,
                "failed_postconditions": list(run.failed_keys),
                "result": str(run.result_path),
                "result_sha256": run.result_sha256,
            }
        )
        if run.status is not LiveState.PASS:
            worst = EXIT_FAILURE
            if not as_json:
                _render_failure(
                    printer, scenario, run.status.value, run.failure_code, run.failed_keys
                )
            break
        if not as_json:
            printer.line(f"{scenario.id}  PASS  attempt {run.attempt}")
    if as_json:
        printer.json({"commit": commit, "runs": records})
    return worst


def _render_failure(
    printer: Printer,
    scenario: LiveScenario,
    status: str,
    failure_code: str,
    failed_keys: Sequence[str],
) -> None:
    printer.line(f"{scenario.id}  {status}  {failure_code}")
    for key in failed_keys:
        condition = scenario.postcondition(key)
        statement = "" if condition is None else f" — {condition.statement}"
        printer.line(f"    unmet: {key}{statement}")
    printer.field("suspect", scenario.suspect_module)
    printer.field("logs", ", ".join(scenario.logs))


def _selection(store: StateStore, only: Sequence[str] | None) -> tuple[LiveScenario, ...]:
    """Which scenarios a run covers.

    An explicit id runs even when it already passed — a re-run is legitimate and
    is recorded as an extra attempt. Without ids, only what is still pending
    runs, so a batch does not spend an operator's evening re-proving S01.
    """
    if only:
        return resolve(only)
    return tuple(
        scenario
        for scenario in resolve(None)
        if store.read(scenario.id).state is not LiveState.PASS
    )


def _status(
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    verbose: bool,
    as_json: bool,
) -> int:
    states = store.read_all(SCENARIO_IDS)
    tally = summarise(states)
    if as_json:
        printer.json(
            {
                "evidence_root": str(layout.root),
                "counts": tally,
                "scenarios": [state.to_dict() for state in states],
            }
        )
        return EXIT_OK if tally[LiveState.PASS.value] == len(SCENARIO_IDS) else EXIT_FAILURE

    printer.heading(f"live-test status {layout.root}")
    width = max(len(scenario_id) for scenario_id in SCENARIO_IDS)
    for state in states:
        last = "never" if state.last_run_ms is None else _stamp(state.last_run_ms)
        attempts = f"{state.attempt_count} attempt(s)" if state.attempts else "-"
        printer.line(
            f"  {state.scenario_id:<{width}}  {state.state.value:<8}  {attempts:<12}  {last}"
        )
        if verbose:
            printer.lines(attempt_lines(state))
    printer.line("")
    printer.line(
        f"  PASS {tally['PASS']}   FAIL {tally['FAIL']}   "
        f"BLOCKED {tally['BLOCKED']}   NOT_RUN {tally['NOT_RUN']}"
    )
    if tally["NOT_RUN"] == len(SCENARIO_IDS):
        # Counted, not spelled out: this line said "All twenty" while the tally
        # printed directly above it said 22, because the crafting and building
        # wave added two scenarios and a hardcoded word cannot follow a list.
        printer.line(f"  Nothing has been exercised. All {len(SCENARIO_IDS)} need a running game.")
    return EXIT_OK if tally[LiveState.PASS.value] == len(SCENARIO_IDS) else EXIT_FAILURE


def _stamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat(timespec="seconds")


def _collect(
    ctx: CliContext,
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    only: Sequence[str] | None,
    as_json: bool,
) -> int:
    """Copy the diagnostics a scenario declares into its own folder.

    Copies rather than moves, and hashes each file as it lands: the exchange
    directory keeps being written to, so an artefact that is not pinned by
    content is a moving target by the time anybody reads it.
    """
    workspace = resolve_workspace(ctx)
    scenarios = (
        resolve(only)
        if only
        else tuple(
            scenario
            for scenario in resolve(None)
            if store.read(scenario.id).state is not LiveState.NOT_RUN
        )
    )
    if not scenarios:
        printer.line("nothing to collect: no scenario has run yet.")
        return EXIT_OK

    reports: list[JsonDict] = []
    for scenario in scenarios:
        sources = _collection_sources(workspace, layout, scenario)
        report = collect_files(sources, scenario_id=scenario.id)
        write_document(layout.collected_path(scenario.id), report.to_dict(), schema=None)
        reports.append(report.to_dict())
        if not as_json:
            printer.line(
                f"{scenario.id}: copied {len(report.copied)} file(s)"
                + (f", skipped {len(report.skipped)}" if report.skipped else "")
            )
            printer.lines(f"    skipped {entry}" for entry in report.skipped)
    if as_json:
        printer.json({"collected": reports, "index": COLLECTED_NAME})
    return EXIT_OK


def _collection_sources(
    workspace: Workspace, layout: EvidenceLayout, scenario: LiveScenario
) -> list[tuple[Path, Path]]:
    """Every ``(source, destination)`` pair for one scenario.

    Built from the scenario's declared log list so a scenario that never asked
    for the queue journal does not accumulate one — and, more usefully, so a
    declared log that is *absent* is reported by name instead of never being
    looked for.
    """
    ipc_root = workspace.ipc_root
    user_dir = workspace.user_dir
    pairs: list[tuple[Path, Path]] = []
    logs_dir = layout.logs_dir(scenario.id)
    for name in scenario.logs:
        if name == _GAME_CONSOLE and user_dir is not None:
            pairs.append((user_dir / _GAME_CONSOLE, logs_dir / name))
        elif name in _SIDECAR_LOG_NAMES:
            pairs.append((workspace.logs_dir / name, logs_dir / name))
        elif ipc_root is not None:
            pairs.append((ipc_root / name, logs_dir / name))
    if ipc_root is not None:
        for name in _JOURNAL_NAMES:
            pairs.append((ipc_root / name, layout.journals_dir(scenario.id) / name))
        for name in (*_SNAPSHOT_NAMES, *_HEARTBEAT_NAMES):
            pairs.append((ipc_root / name, layout.snapshots_dir(scenario.id) / name))
    # The trace, unconditionally: it is what ``pz-agent replay`` reads, and no
    # scenario declares it because none existed when the lists were written.
    # The current file is named rather than globbed, so its absence is reported
    # like any other declared file; the rotated generations are globbed, because
    # a scenario short enough not to rotate is not missing anything.
    pairs.append((workspace.trace_dir / TRACE_NAME, logs_dir / TRACE_NAME))
    pairs.extend(
        (rotated, logs_dir / rotated.name)
        for rotated in sorted(workspace.trace_dir.glob(f"{TRACE_NAME}.*"))
    )
    return pairs


def _finalize(
    layout: EvidenceLayout,
    store: StateStore,
    printer: Printer,
    *,
    output: Path | None,
    as_json: bool,
) -> int:
    """Build the manifest, or refuse and name everything that is wrong.

    The refusal lists every problem at once. Reporting only the first would make
    an operator run this once per problem to learn one fact at a time, and each of
    those
    runs is a chance to decide the gate is the obstacle.
    """
    destination = output or default_manifest_path()
    try:
        path, document = build_manifest(
            layout=layout,
            store=store,
            scenarios=resolve(None),
            output=destination,
            commit=read_commit(repo_root()),
        )
    except FinalizeRefused as exc:
        if as_json:
            printer.json(
                {
                    "written": False,
                    "not_passed": list(exc.not_passed),
                    "missing": list(exc.missing),
                    "tampered": list(exc.tampered),
                }
            )
        else:
            printer.lines(exc.render_lines())
        return EXIT_FAILURE
    if as_json:
        printer.json({"written": True, "path": str(path), "manifest": document})
        return EXIT_OK
    totals = document["totals"]
    printer.line(f"wrote {path}")
    printer.field("scenarios", str(document["scenario_count"]))
    printer.field("artefacts", f"{totals['artefact_count']} file(s), {totals['bytes']} bytes")
    return EXIT_OK
