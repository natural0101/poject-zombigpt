"""The layout is the only source of filenames; nothing may name a file itself."""

from __future__ import annotations

from pathlib import Path

from pz_agent_core.ipc.layout import SESSION_FILE, IpcLayout, SnapshotSlot, temp_name


def test_filenames_match_the_protocol_chapter(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path)
    names = {path.name for path in layout.managed_paths()}
    assert {
        "session.json",
        "capabilities.json",
        "observation.snapshot.a.json",
        "observation.snapshot.b.json",
        "observation.snapshot.pointer",
        "observation.events.0001.jsonl",
        "command.queue.0001.jsonl",
        "command.ack.0001.jsonl",
        "heartbeat.game.json",
        "heartbeat.sidecar.json",
        "panic.stop",
        "logs",
    } <= names


def test_slots_alternate() -> None:
    assert SnapshotSlot.A.other is SnapshotSlot.B
    assert SnapshotSlot.B.other is SnapshotSlot.A


def test_managed_paths_are_accepted(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path)
    for path in layout.managed_paths():
        assert layout.is_managed_path(path), path


def test_temporary_rotated_and_log_paths_are_accepted(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path)
    assert layout.is_managed_path(layout.session.with_name("session.json.tmp"))
    assert layout.is_managed_path(layout.command_ack.with_name("command.ack.0001.jsonl.3"))
    assert layout.is_managed_path(layout.logs_dir / "executor.log")


def test_the_scratch_name_of_3_5_is_a_managed_path(tmp_path: Path) -> None:
    """§3.5 names the scratch file ``filename.tmp.<pid>.<seq>``."""
    layout = IpcLayout(tmp_path)
    scratch = temp_name(SESSION_FILE, 4321, 7)
    assert scratch == "session.json.tmp.4321.7"
    assert layout.is_managed_path(layout.root / scratch)
    assert layout.is_managed_path(layout.root / temp_name("observation.snapshot.pointer", 1, 0))
    # The pid/seq decoration does not turn a foreign name into a managed one.
    assert not layout.is_managed_path(layout.root / temp_name("evil.json", 4321, 7))
    assert not layout.is_managed_path(layout.root / "session.json.tmp.notapid.7")


def test_arbitrary_and_traversing_paths_are_rejected(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path)
    assert not layout.is_managed_path(tmp_path / "evil.json")
    assert not layout.is_managed_path(tmp_path / ".." / "session.json")
    assert not layout.is_managed_path(tmp_path / "sub" / "session.json")
    assert not layout.is_managed_path(tmp_path)
    # A rotation-looking suffix on a name the layout does not own stays rejected.
    assert not layout.is_managed_path(tmp_path / "other.jsonl.1")


def test_symlink_out_of_the_directory_is_rejected(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path / "ipc")
    layout.ensure()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = layout.root / "session.json"
    link.symlink_to(outside)
    assert not layout.is_managed_path(link)


def test_ensure_creates_the_directory_and_logs(tmp_path: Path) -> None:
    layout = IpcLayout(tmp_path / "pz_agent")
    layout.ensure()
    assert layout.root.is_dir()
    assert layout.logs_dir.is_dir()
    layout.ensure()  # idempotent
    assert layout.logs_dir.is_dir()
