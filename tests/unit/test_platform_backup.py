"""Save backup: manifests, hash-verified restore, and the rules around both."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pz_agent_core.platform.backup import (
    HOME_PLACEHOLDER,
    MANIFEST_NAME,
    REDACTED_PLACEHOLDER,
    USER_DIR_PLACEHOLDER,
    BackupCorruptError,
    BackupError,
    BackupManager,
    BackupNotFoundError,
    BackupRecord,
    BackupTooLargeError,
    GameRunningError,
    SaveNotFoundError,
    _copy_and_hash,
)
from pz_agent_core.version import PRODUCT_VERSION, SCHEMA_VERSION
from tests.fixtures.platform_trees import (
    CYRILLIC_USER,
    FakeClock,
    make_save,
    make_user_dir,
    read_save,
)

SAVE_ID = "Survivor/09-07-1993"

SAVE_FILES: dict[str, bytes] = {
    "map_t.bin": b"tiles" * 32,
    "players.db": b"player-state",
    "chunkdata/map_100_100.bin": b"chunk" * 16,
    "Дневник.txt": "жив ещё\n".encode(),
}


def _manager(tmp_path: Path, **kwargs: object) -> tuple[BackupManager, Path]:
    user_dir = make_user_dir(tmp_path / "Users" / CYRILLIC_USER)
    manager = BackupManager(
        user_dir,
        tmp_path / "backups",
        clock=FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )
    return manager, user_dir


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_backup_roundtrip_records_every_file_with_a_hash(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    source = make_save(user_dir, SAVE_ID, SAVE_FILES)

    record = manager.create(SAVE_ID)

    assert record.save_id == SAVE_ID
    assert record.file_count == len(SAVE_FILES)
    assert record.total_bytes == sum(len(payload) for payload in SAVE_FILES.values())
    assert record.product_version == PRODUCT_VERSION
    assert record.schema_version == SCHEMA_VERSION
    assert {entry.path for entry in record.files} == set(SAVE_FILES)
    for entry in record.files:
        assert len(entry.sha256) == 64
        assert entry.size == len(SAVE_FILES[entry.path])
    assert read_save(record.data_dir) == read_save(source)
    assert manager.verify(record.backup_id).backup_id == record.backup_id


def test_manifest_is_valid_json_and_hides_the_users_home(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    record = manager.create(SAVE_ID)

    payload = json.loads((record.directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["backup_id"] == record.backup_id
    assert payload["file_count"] == record.file_count
    assert payload["source_dir"].startswith(USER_DIR_PLACEHOLDER)
    assert CYRILLIC_USER not in payload["source_dir"]
    assert str(tmp_path) not in payload["source_dir"]
    reparsed = BackupRecord.from_dict(payload, directory=record.directory)
    assert reparsed == record


def test_source_outside_the_user_dir_falls_back_to_the_home_placeholder(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    assert manager._redact(user_dir.parent / "elsewhere" / "thing") == (
        f"{HOME_PLACEHOLDER}/elsewhere/thing"
    )
    assert manager._redact(user_dir) == USER_DIR_PLACEHOLDER
    # A path under neither keeps only its basename, so nothing above it leaks.
    assert manager._redact(tmp_path / "unrelated" / "secret.bin") == (
        f"{REDACTED_PLACEHOLDER}/secret.bin"
    )


def test_backup_id_is_time_sortable_and_unique_within_one_second(tmp_path: Path) -> None:
    clock = FakeClock()
    clock.freeze()
    user_dir = make_user_dir(tmp_path / "home")
    manager = BackupManager(user_dir, tmp_path / "backups", clock=clock)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    first = manager.create(SAVE_ID)
    second = manager.create(SAVE_ID)

    assert first.backup_id != second.backup_id
    assert first.backup_id.startswith("20260805T120000Z-")
    assert second.backup_id.startswith(first.backup_id)


def test_missing_save_is_reported(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)

    with pytest.raises(SaveNotFoundError, match="no save directory"):
        manager.create(SAVE_ID)


def test_empty_save_directory_is_refused(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, {})

    with pytest.raises(BackupError, match="contains no files"):
        manager.create(SAVE_ID)


@pytest.mark.parametrize(
    "save_id",
    ["", "..", "../../etc", "Survivor/../../..", "C:/Windows", "a/b/c/d/e", "x" * 200],
)
def test_unsafe_save_ids_are_rejected(tmp_path: Path, save_id: str) -> None:
    manager, _ = _manager(tmp_path)

    with pytest.raises(BackupError):
        manager.create(save_id)


def test_size_cap_refuses_before_writing_anything(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path, max_bytes=16)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    with pytest.raises(BackupTooLargeError, match="byte cap"):
        manager.create(SAVE_ID)

    assert list((tmp_path / "backups").glob("*")) == []
    assert manager.list_backups() == ()


def test_file_count_cap_is_enforced(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path, max_files=2)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    with pytest.raises(BackupTooLargeError, match="more than 2 files"):
        manager.create(SAVE_ID)


def test_symlink_inside_a_save_is_refused(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    secret = tmp_path / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")
    (save_dir / "link.txt").symlink_to(secret)

    with pytest.raises(BackupError, match="symbolic links"):
        manager.create(SAVE_ID)


def test_copy_refuses_a_file_that_exceeds_the_remaining_budget(tmp_path: Path) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"a" * 4096)

    with pytest.raises(BackupTooLargeError, match="remaining"):
        _copy_and_hash(source, tmp_path / "out.bin", limit=100)


def test_manager_rejects_nonsense_caps(tmp_path: Path) -> None:
    user_dir = make_user_dir(tmp_path / "home")
    with pytest.raises(BackupError, match="max_bytes"):
        BackupManager(user_dir, tmp_path / "backups", max_bytes=0)
    with pytest.raises(BackupError, match="max_files"):
        BackupManager(user_dir, tmp_path / "backups", max_files=0)


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


def test_list_backups_is_newest_first_and_ignores_junk(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    first = manager.create(SAVE_ID)
    second = manager.create(SAVE_ID)

    backup_root = tmp_path / "backups"
    (backup_root / ".staging-leftover").mkdir()
    (backup_root / "not-a-backup").mkdir()
    broken = backup_root / "broken"
    broken.mkdir()
    (broken / MANIFEST_NAME).write_text("{ not json", encoding="utf-8")

    listed = manager.list_backups()

    assert [record.backup_id for record in listed] == [second.backup_id, first.backup_id]


def test_get_rejects_unknown_and_malformed_ids(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)

    with pytest.raises(BackupNotFoundError, match="no backup"):
        manager.get("20260805T120000Z-nope")
    with pytest.raises(BackupNotFoundError, match="malformed"):
        manager.get("../escape")


def test_manifest_that_is_not_an_object_is_corrupt(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    (record.directory / MANIFEST_NAME).write_text("[]", encoding="utf-8")

    with pytest.raises(BackupCorruptError, match="not a JSON object"):
        manager.get(record.backup_id)


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"files": "nope"}, "no 'files' array"),
        ({"total_bytes": -1}, "total_bytes"),
        ({"save_id": ""}, "save_id"),
        ({"files": [{"path": "a", "size": "big", "sha256": "x"}]}, "non-numeric size"),
        ({"files": [{"path": "a", "size": 1}]}, "missing 'sha256'"),
        ({"files": [{"path": "../a", "size": 1, "sha256": "x"}]}, "escapes the backup"),
        ({"files": [{"path": "/a", "size": 1, "sha256": "x"}]}, "not a relative path"),
        ({"files": ["a"]}, "non-object entry"),
    ],
)
def test_corrupt_manifests_are_rejected(
    tmp_path: Path, mutation: dict[str, object], fragment: str
) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    manifest = record.directory / MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.update(mutation)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupCorruptError, match=fragment):
        manager.get(record.backup_id)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_is_refused_while_the_game_is_running(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    (save_dir / "map_t.bin").write_bytes(b"live game state")
    before = read_save(save_dir)

    with pytest.raises(GameRunningError, match="close the game"):
        manager.restore(record.backup_id, game_running=True)

    assert read_save(save_dir) == before


def test_restore_puts_the_save_back(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    original = read_save(save_dir)

    (save_dir / "map_t.bin").write_bytes(b"ruined")
    (save_dir / "junk.tmp").write_bytes(b"left over")
    (save_dir / "players.db").unlink()

    result = manager.restore(record.backup_id, game_running=False)

    assert result.target_dir == save_dir
    assert result.file_count == len(SAVE_FILES)
    assert result.total_bytes == record.total_bytes
    assert read_save(save_dir) == original


def test_restore_into_a_deleted_save_directory(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    original = read_save(save_dir)
    shutil.rmtree(save_dir.parent)

    manager.restore(record.backup_id, game_running=False)

    assert read_save(save_dir) == original


def test_hash_mismatch_is_detected_before_anything_is_written(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    (save_dir / "map_t.bin").write_bytes(b"current state")
    before = read_save(save_dir)

    corrupted = record.data_dir / "map_t.bin"
    corrupted.write_bytes(b"t" * corrupted.stat().st_size)

    with pytest.raises(BackupCorruptError, match="failed its SHA-256 check"):
        manager.restore(record.backup_id, game_running=False)

    assert read_save(save_dir) == before


def test_size_mismatch_is_detected(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    (record.data_dir / "map_t.bin").write_bytes(b"short")

    with pytest.raises(BackupCorruptError, match="manifest says"):
        manager.verify(record.backup_id)


def test_missing_and_extra_files_are_both_corruption(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    missing = manager.create(SAVE_ID)
    (missing.data_dir / "players.db").unlink()
    with pytest.raises(BackupCorruptError, match="missing"):
        manager.restore(missing.backup_id, game_running=False)

    extra = manager.create(SAVE_ID)
    (extra.data_dir / "smuggled.bin").write_bytes(b"x")
    with pytest.raises(BackupCorruptError, match="not in the manifest"):
        manager.restore(extra.backup_id, game_running=False)


def test_manifest_save_id_cannot_redirect_the_restore(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)
    manifest = record.directory / MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["save_id"] = "../../../Lua"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupError, match="unusable component"):
        manager.restore(record.backup_id, game_running=False)

    assert (user_dir / "Lua").is_dir()
    assert not any((user_dir / "Lua").iterdir())


def test_nested_directories_survive_the_roundtrip(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)

    assert (record.data_dir / "chunkdata" / "map_100_100.bin").is_file()

    (save_dir / "chunkdata" / "map_100_100.bin").write_bytes(b"broken")
    manager.restore(record.backup_id, game_running=False)

    assert (save_dir / "chunkdata" / "map_100_100.bin").read_bytes() == b"chunk" * 16


def test_restore_leaves_no_staging_directories_behind(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    save_dir = make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)

    manager.restore(record.backup_id, game_running=False)

    leftovers = [path.name for path in save_dir.parent.iterdir() if path.name.startswith(".")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_prune_keeps_the_newest_and_returns_what_it_removed(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    records = [manager.create(SAVE_ID) for _ in range(5)]

    removed = manager.prune(2)

    assert set(removed) == {record.backup_id for record in records[:3]}
    kept = [record.backup_id for record in manager.list_backups()]
    assert kept == [records[4].backup_id, records[3].backup_id]
    for record in records[:3]:
        assert not record.directory.exists()


def test_prune_never_deletes_the_newest(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    newest = [manager.create(SAVE_ID) for _ in range(3)][-1]

    manager.prune(1)

    assert [record.backup_id for record in manager.list_backups()] == [newest.backup_id]
    assert manager.verify(newest.backup_id).file_count == len(SAVE_FILES)


def test_prune_below_one_is_refused(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID)

    for keep in (0, -1):
        with pytest.raises(BackupError, match="at least one"):
            manager.prune(keep)

    assert manager.get(record.backup_id).backup_id == record.backup_id


def test_prune_with_fewer_backups_than_kept_is_a_no_op(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    manager.create(SAVE_ID)

    assert manager.prune(10) == ()
    assert len(manager.list_backups()) == 1


def test_prune_on_an_empty_backup_root_does_nothing(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)

    assert manager.prune(3) == ()
    assert manager.list_backups() == ()
