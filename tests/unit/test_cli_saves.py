"""``backup-save`` and ``restore-save``: the commands that can lose a save.

The interesting question here is not whether a restore copies bytes — that is
``tests/unit/test_platform_backup.py``'s job — but what the command believes
about the game before it starts. ``BackupManager.restore`` refuses to decide
that for itself, so whatever this module decides *is* the safety check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_cli import saves
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK
from pz_agent_cli.supervisor import ProcessLister, ProcessListing
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.platform.backup import BackupManager
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import MOD_VERSION
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import make_save

SAVE_ID = "Survivor/09-07-1993"

SAVE_FILES = {"map_t.bin": b"tiles", "players.db": b"state"}

#: What the live game left in the save directory. If a restore runs, this is
#: what gets overwritten, so its survival is the assertion that matters.
LIVE_STATE = b"live game state"


def _fixed_lister(*names: str, problem: str = "") -> ProcessLister:
    """A process lister that reports exactly what a test wants it to."""

    def lister() -> ProcessListing:
        return ProcessListing(names=names, problem=problem)

    return lister


def _backed_up_world(tmp_path: Path) -> tuple[CliWorld, Path, str]:
    """A world with one save, one verified backup, and a dirtied save directory."""
    world = make_world(tmp_path)
    assert world.user_dir is not None
    save_dir = make_save(world.user_dir, SAVE_ID, SAVE_FILES)
    assert world.run("backup-save") == EXIT_OK
    backup_id = world.stdout.split(" as ")[1].split()[0]
    world.reset_streams()
    (save_dir / "map_t.bin").write_bytes(LIVE_STATE)
    return world, save_dir, backup_id


def test_restore_is_refused_when_a_game_process_is_running_without_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat is not the only evidence, and it is the weakest one.

    A game whose mod is disabled, or which is sitting on the main menu, writes
    no heartbeat at all. The process table still names it, and restoring over a
    live save directory is the one mistake this project cannot undo.
    """
    world, save_dir, backup_id = _backed_up_world(tmp_path)
    monkeypatch.setattr(
        saves,
        "list_processes",
        _fixed_lister("explorer.exe", "ProjectZomboid64.exe"),
    )

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_FAILURE
    assert (save_dir / "map_t.bin").read_bytes() == LIVE_STATE
    assert "Project Zomboid" in world.stderr


def test_restore_is_refused_when_the_process_list_could_not_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "We could not tell" is not permission. It has to collapse toward refusing."""
    world, save_dir, backup_id = _backed_up_world(tmp_path)
    monkeypatch.setattr(
        saves,
        "list_processes",
        _fixed_lister(problem="ps is not available on this machine"),
    )

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_FAILURE
    assert (save_dir / "map_t.bin").read_bytes() == LIVE_STATE
    assert "ps is not available" in world.stderr


def test_restore_runs_when_the_process_table_shows_no_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive case, and the evidence it rests on is reported."""
    world, save_dir, backup_id = _backed_up_world(tmp_path)
    monkeypatch.setattr(saves, "list_processes", _fixed_lister("explorer.exe", "python"))

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_OK
    assert (save_dir / "map_t.bin").read_bytes() == SAVE_FILES["map_t.bin"]
    assert "running process(es) is Project Zomboid" in world.stderr


def test_restore_refuses_first_on_the_heartbeat_without_listing_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live heartbeat is proof; no external command should be needed to act on it."""
    world, save_dir, backup_id = _backed_up_world(tmp_path)

    def refuse() -> ProcessListing:
        raise AssertionError("the process table must not be consulted after proof")

    monkeypatch.setattr(saves, "list_processes", refuse)
    _publish_game_heartbeat(world)

    exit_code = world.run("restore-save", backup_id)

    assert exit_code == EXIT_FAILURE
    assert (save_dir / "map_t.bin").read_bytes() == LIVE_STATE
    assert "heartbeat" in world.stderr


def test_the_json_form_carries_the_verdict_the_restore_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _, backup_id = _backed_up_world(tmp_path)
    monkeypatch.setattr(saves, "list_processes", _fixed_lister("explorer.exe"))

    assert world.run("restore-save", backup_id, "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert document["game_probe"]["verdict"] == "not_running"
    assert document["game_probe"]["unsafe_to_restore"] is False


def test_a_save_file_vanishing_mid_backup_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` promises never to raise for a user error, and this is one.

    The game is allowed to be running while a backup is taken — the quickstart
    has the user take one before installing the mod — so a chunk file rotating
    away between the plan and the copy is an ordinary event, not a bug to be
    reported as a stack trace.
    """
    world = make_world(tmp_path)
    assert world.user_dir is not None
    make_save(world.user_dir, SAVE_ID, SAVE_FILES)
    real_plan = BackupManager._plan

    def plan_then_delete(self: BackupManager, source: Path) -> tuple[tuple[Path, int], ...]:
        planned = real_plan(self, source)
        (source / "players.db").unlink()
        return planned

    monkeypatch.setattr(BackupManager, "_plan", plan_then_delete)

    assert world.run("backup-save") == EXIT_FAILURE
    assert "backup failed" in world.stderr
    assert "changed while it was being backed up" in world.stderr


def _publish_game_heartbeat(world: CliWorld) -> None:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    HeartbeatMonitor(layout, clock=lambda: world.clock.now_ms).publish(
        Peer.GAME,
        session_id="sess-restore",
        nonce="nonce-1",
        version=MOD_VERSION,
        build="42.20",
        player_present=True,
        armed=False,
        mode=SessionMode.OBSERVE,
    )
