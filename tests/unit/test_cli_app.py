"""The parser, and the commands whose subsystems are wired: logs, replay, status, saves."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from pz_agent_cli import saves as saves_module
from pz_agent_cli.app import COMMANDS, build_parser, main
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, resolve_workspace
from pz_agent_cli.saves import MAX_LISTED_SAVES, find_saves
from pz_agent_cli.supervisor import ProcessListing
from pz_agent_cli.support import DEFAULT_LOG_LINES, MAX_LOG_LINES, _add_directory
from pz_agent_core.diagnostics import BundleBuilder, DiagnosticLog, LogLevel, TraceWriter
from pz_agent_core.diagnostics.log import LogLimits
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.observation.diff import diff_observations
from pz_agent_core.protocol import ActionName, ActionResult, Command, SessionMode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import CYRILLIC_USER, make_save

SAVE_FILES = {"map_t.bin": b"tiles", "players.db": b"state"}


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def test_every_declared_command_is_accepted_by_the_parser() -> None:
    parser = build_parser()

    for command in COMMANDS:
        argv = [command, *_REQUIRED_ARGUMENT.get(command, ())]
        assert parser.parse_args(argv).command == command


#: The two commands that take a positional argument, so the round-trip above can
#: parse every command without special-casing inside the loop.
_REQUIRED_ARGUMENT = {"restore-save": ("some-backup-id",), "replay": ("trace.jsonl",)}


def test_a_command_with_no_subsystem_is_rejected_rather_than_stubbed(tmp_path: Path) -> None:
    """Better an "invalid choice" than a subcommand that prints "not implemented"."""
    world = make_world(tmp_path)

    with pytest.raises(SystemExit) as caught:
        world.run("setup")

    assert caught.value.code == EXIT_USAGE


@pytest.mark.parametrize("command", ["stop", "arm", "disarm"])
def test_a_sidecar_command_reports_that_nothing_is_running(tmp_path: Path, command: str) -> None:
    """The subsystem exists; what is absent is a process, and that is said plainly."""
    world = make_world(tmp_path)

    exit_code = world.run(command)

    assert exit_code == EXIT_FAILURE
    assert "no sidecar" in world.stderr


def test_no_command_prints_the_help_and_reports_a_usage_error(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    exit_code = world.run()

    assert exit_code == EXIT_USAGE
    assert "usage: pz-agent" in world.stdout
    assert "doctor" in world.stdout


def test_the_version_flag_reports_the_product_version(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["--version"], context=world.ctx)

    assert caught.value.code == EXIT_OK


def test_the_state_directory_override_moves_every_artefact(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    elsewhere = tmp_path / "elsewhere"

    world.run("--state-dir", str(elsewhere), "doctor")

    assert (elsewhere / "compat" / "generated_api_report.json").is_file()


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _write_log(world: CliWorld) -> DiagnosticLog:
    workspace = resolve_workspace(world.ctx)
    log = DiagnosticLog(workspace.logs_dir, redactor=workspace.redactor, clock=world.clock)
    for index in range(5):
        log.log(LogLevel.INFO, "session.tick", index=index)
    return log


def test_logs_prints_the_tail_of_the_human_log(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _write_log(world)

    assert world.run("logs", "--lines", "2") == EXIT_OK

    printed = [line for line in world.stdout.splitlines() if line.strip()]
    assert len(printed) == 2
    assert "session.tick" in printed[-1]


def test_logs_json_returns_the_structured_records(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _write_log(world)

    assert world.run("logs", "--json", "--lines", "3") == EXIT_OK

    document = json.loads(world.stdout)
    assert len(document["records"]) == 3
    assert document["records"][0]["event"] == "session.tick"
    assert document["problems"] == []


def test_logs_says_so_when_nothing_has_been_written_yet(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("logs") == EXIT_OK
    assert "no log yet" in world.stdout


def test_a_log_that_exists_but_cannot_be_read_is_not_reported_as_absent(
    tmp_path: Path,
) -> None:
    """ "No log yet" over a log that is there is the wrong diagnosis.

    A directory in the log file's place is the portable stand-in for a locked or
    unreadable file: both surface as an ``OSError`` that is not
    ``FileNotFoundError``.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    (workspace.logs_dir / "pz-agent.log").mkdir(parents=True)

    exit_code = world.run("logs")

    assert exit_code == EXIT_FAILURE
    assert "no log yet" not in world.stdout
    assert "could not be read" in world.stderr


@pytest.mark.parametrize("lines", ["0", str(MAX_LOG_LINES + 1)])
def test_logs_refuses_a_line_count_outside_the_bound(tmp_path: Path, lines: str) -> None:
    world = make_world(tmp_path)

    assert world.run("logs", "--lines", lines) == EXIT_USAGE
    assert f"between 1 and {MAX_LOG_LINES}" in world.stderr


def test_verify_without_bundle_is_a_usage_error(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("logs", "--verify") == EXIT_USAGE
    assert "pass --bundle as well" in world.stderr


def test_the_default_line_count_is_the_documented_one() -> None:
    assert build_parser().parse_args(["logs"]).lines == DEFAULT_LOG_LINES


# ---------------------------------------------------------------------------
# the support bundle
# ---------------------------------------------------------------------------


def test_the_bundle_carries_what_the_blueprint_lists(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _write_log(world)
    _write_journal(world)
    destination = tmp_path / "support.zip"

    assert world.run("logs", "--bundle", "--output", str(destination)) == EXIT_OK

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
    assert {"versions.json", "doctor.json", "capabilities.json", "manifest.json"} <= names
    assert "logs/pz-agent.log" in names
    assert f"ipc/{IpcLayout(Path('x')).command_ack.name}" in names


def test_the_bundle_holds_no_save_and_no_game_source(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)
    destination = tmp_path / "support.zip"

    world.run("logs", "--bundle", "--output", str(destination))

    with zipfile.ZipFile(destination) as archive:
        blob = b"".join(archive.read(name) for name in archive.namelist())
    assert b"players.db" not in blob
    assert b"ISBaseTimedAction.new" not in blob


def test_a_log_the_bundle_had_no_room_for_is_named_with_the_real_reason(
    tmp_path: Path,
) -> None:
    """A bound breach and a locked file are different problems.

    Both arrive as a ``BundleError``; reporting the bound as "could not be read"
    would send a user looking for a permissions fault that is not there.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_text("first\n", encoding="utf-8")
    (logs / "b.log").write_text("x" * 5000 + "\n", encoding="utf-8")
    builder = BundleBuilder(max_total_bytes=400)

    _add_directory(builder, "logs", logs, ("*.log",))
    bundle = builder.build(tmp_path / "partial.zip")

    assert [entry.name for entry in bundle.entries] == ["logs/a.log", "logs/b.log.omitted.txt"]
    with zipfile.ZipFile(bundle.path) as archive:
        note = archive.read("logs/b.log.omitted.txt").decode("utf-8")
    assert "400 byte cap" in note
    assert "could not be read" not in note


def test_verify_lists_the_contents_and_reports_them_clean(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _write_log(world)
    destination = tmp_path / "support.zip"

    exit_code = world.run("logs", "--bundle", "--verify", "--output", str(destination))

    assert exit_code == EXIT_OK
    assert "doctor.json" in world.stdout
    assert "clean" in world.stdout
    assert "nothing matching a home directory" in world.stdout
    assert CYRILLIC_USER not in world.stdout


def test_the_bundle_json_output_names_the_members(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    destination = tmp_path / "support.zip"

    assert world.run("logs", "--bundle", "--json", "--output", str(destination)) == EXIT_OK

    document = json.loads(world.stdout)
    assert document["product_version"] == PRODUCT_VERSION
    assert any(entry["name"] == "doctor.json" for entry in document["entries"])


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _write_trace(world: CliWorld, path: Path) -> Path:
    workspace = resolve_workspace(world.ctx)
    writer = TraceWriter(
        path,
        redactor=workspace.redactor,
        clock=world.clock,
        limits=LogLimits(max_bytes=1024 * 1024, keep=1, max_record_bytes=512 * 1024),
    )
    first = make_observation(seq=1)
    second = make_observation(seq=2, player=make_player(stats={"hunger": 0.6}))
    writer.record_observation(first)
    writer.record_observation_diff(from_seq=1, to_seq=2, diff=diff_observations(first, second))
    command = Command(
        session_id=DEFAULT_SESSION,
        seq=3,
        command_id="00000000-0000-0000-0000-0000000c0de0",
        idempotency_key="k1",
        issued_at_ms=1,
        lease_ms=15_000,
        action=ActionName.ACTION_WAIT,
    )
    writer.record_action(
        command,
        ActionResult.succeeded(
            session_id=DEFAULT_SESSION,
            seq=4,
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=2,
            evidence={"waited_ms": 500},
        ),
    )
    return path


def test_replay_walks_the_entries_and_rebuilds_the_observations(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    trace = _write_trace(world, tmp_path / "trace.jsonl")

    assert world.run("replay", str(trace)) == EXIT_OK

    assert "observation full seq=1" in world.stdout
    assert "observation diff 1->2" in world.stdout
    assert "action action.wait -> succeeded" in world.stdout
    assert "observations rebuilt   2" in world.stdout


def test_replay_json_reports_the_entries_and_the_rebuild_count(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    trace = _write_trace(world, tmp_path / "trace.jsonl")

    assert world.run("replay", str(trace), "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert document["entry_count"] == 3
    assert document["observations_rebuilt"] == 2
    assert document["replay_problem"] == ""


def test_replay_limits_what_it_prints_and_says_how_many_remain(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    trace = _write_trace(world, tmp_path / "trace.jsonl")

    world.run("replay", str(trace), "--limit", "1")

    assert "2 further entry(ies) not shown" in world.stdout


def test_replay_of_a_missing_file_fails_with_the_reason(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("replay", str(tmp_path / "absent.jsonl")) == EXIT_FAILURE
    assert "cannot read trace" in world.stderr


def test_replay_reports_a_trace_it_cannot_faithfully_rebuild(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    trace = tmp_path / "orphan.jsonl"
    first = make_observation(seq=1)
    second = make_observation(seq=2, player=make_player(stats={"hunger": 0.6}))
    writer = TraceWriter(trace, clock=world.clock)
    writer.record_observation_diff(from_seq=1, to_seq=2, diff=diff_observations(first, second))

    assert world.run("replay", str(trace)) == EXIT_FAILURE
    assert "could not be replayed" in world.stderr


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_an_absent_exchange_directory_without_inventing_a_state(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)

    assert world.run("status") == EXIT_OK
    assert "nothing has ever connected" in world.stdout
    assert "pz-agent doctor" in world.stdout


def test_status_reports_a_live_game_with_its_mode_and_build(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    assert world.run("status") == EXIT_OK

    assert "build                  42.20" in world.stdout
    assert "armed                  no" in world.stdout
    assert f"mode                   {SessionMode.OBSERVE.value}" in world.stdout


def test_status_json_carries_the_heartbeat_and_the_environment(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    assert world.run("status", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert document["game"]["alive"] is True
    assert document["game"]["heartbeat"]["build"] == "42.20"
    assert document["attached"] is False  # no session file
    assert document["environment"]["mod_version"] == MOD_VERSION
    assert CYRILLIC_USER not in world.stdout


def test_status_without_a_zomboid_directory_is_a_failure(tmp_path: Path) -> None:
    world = make_world(tmp_path, with_user_dir=False)

    assert world.run("status") == EXIT_FAILURE


# ---------------------------------------------------------------------------
# backup-save / restore-save
# ---------------------------------------------------------------------------


def test_the_list_of_saves_offered_to_the_user_is_bounded(tmp_path: Path) -> None:
    """A profile with hundreds of characters must not print hundreds of lines."""
    world = make_world(tmp_path)
    assert world.user_dir is not None
    for index in range(MAX_LISTED_SAVES + 5):
        make_save(world.user_dir, f"Survivor/save-{index:03d}", SAVE_FILES)

    found = find_saves(world.user_dir / "Saves")

    assert len(found) == MAX_LISTED_SAVES
    assert world.run("backup-save") == EXIT_FAILURE
    named = [line for line in world.stderr.splitlines() if line.strip().startswith("Survivor/")]
    assert len(named) == MAX_LISTED_SAVES


def test_a_single_save_is_backed_up_without_being_named(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)

    assert world.run("backup-save") == EXIT_OK
    assert "backed up Survivor/09-07-1993" in world.stdout
    assert "every file re-hashed" in world.stdout


def test_more_than_one_save_must_be_named_rather_than_guessed(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/first", SAVE_FILES)
    make_save(world.user_dir, "Builder/second", SAVE_FILES)

    assert world.run("backup-save") == EXIT_FAILURE
    assert "more than one save exists" in world.stderr
    assert "Builder/second" in world.stderr


def test_backups_are_listed_newest_first_with_their_ids(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)
    world.run("backup-save")
    world.reset_streams()

    assert world.run("backup-save", "--list", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert len(document["backups"]) == 1
    assert document["backups"][0]["save_id"] == "Survivor/09-07-1993"
    assert document["backups"][0]["directory"].startswith("<ZOMBOID>")


def test_restore_refuses_while_the_game_writes_a_heartbeat(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)
    world.run("backup-save")
    backup_id = _first_backup_id(world)
    world.reset_streams()
    _publish_heartbeat(world)

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_FAILURE
    assert "writing a game heartbeat, so the game is open" in world.stderr


def test_restore_states_the_evidence_the_game_was_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-command path, with the process table pinned so it is deterministic.

    The refusal cases live in ``tests/unit/test_cli_saves.py``; this one only
    proves the command still restores when the probe positively reports a closed
    game, and that it says what that verdict rested on.
    """
    world = make_world(tmp_path)
    assert world.user_dir is not None
    save = make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)
    world.run("backup-save")
    backup_id = _first_backup_id(world)
    world.reset_streams()
    (save / "map_t.bin").write_bytes(b"corrupted")
    monkeypatch.setattr(saves_module, "list_processes", lambda: ProcessListing(names=("init",)))

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_OK
    assert (save / "map_t.bin").read_bytes() == b"tiles"
    # Printed after the restore, so it states what was read rather than advising a
    # step the user can no longer take.
    assert "none of the 1 running process(es) is Project Zomboid" in world.stderr
    assert "read before the restore and not during it" in world.stderr


def test_restoring_an_unknown_backup_fails_with_its_id(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)

    assert world.run("restore-save", "20260101T000000Z-nope") == EXIT_FAILURE
    assert "restore failed" in world.stderr


def test_prune_keeps_the_newest_and_reports_what_went(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)
    world.run("backup-save")
    world.run("backup-save")
    world.reset_streams()

    assert world.run("backup-save", "--prune", "1", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert len(document["pruned"]) == 1
    assert document["kept"] == 1


def test_prune_below_one_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, "Survivor/09-07-1993", SAVE_FILES)

    assert world.run("backup-save", "--prune", "0") == EXIT_FAILURE
    assert "at least one backup" in world.stderr


def test_backup_without_a_zomboid_directory_explains_which_check_to_read(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, with_user_dir=False)

    assert world.run("backup-save") == EXIT_FAILURE
    assert "PZD003" in world.stderr


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _first_backup_id(world: CliWorld) -> str:
    workspace = resolve_workspace(world.ctx)
    directories = sorted(path.name for path in workspace.backup_root.iterdir() if path.is_dir())
    assert directories
    return directories[0]


def _publish_heartbeat(world: CliWorld) -> None:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    HeartbeatMonitor(layout, clock=lambda: world.clock.now_ms).publish(
        Peer.GAME,
        session_id=DEFAULT_SESSION,
        nonce="nonce-1",
        version=MOD_VERSION,
        build="42.20",
        player_present=True,
        armed=False,
        mode=SessionMode.OBSERVE,
    )


def _write_journal(world: CliWorld) -> None:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    layout.command_ack.write_text(
        json.dumps({"type": "journal.header", "serial": 1}) + "\n", encoding="utf-8"
    )
