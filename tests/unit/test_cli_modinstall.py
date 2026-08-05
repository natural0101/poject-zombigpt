"""install-mod / uninstall-mod: the round trip, and the files it refuses to touch."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK
from pz_agent_cli.modinstall import (
    MANIFEST_NAME,
    MAX_DEPTH,
    MAX_FILE_BYTES,
    MAX_INSTALL_FILES,
    MOD_ID,
    ForeignFileError,
    InstallError,
    find_source,
    install_mod,
    read_manifest,
    read_mod_info,
    uninstall_mod,
)
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION
from tests.fixtures.cli_worlds import CliWorld, make_mod_source, make_world


def _installed(world: CliWorld) -> Path:
    assert world.mods_dir is not None
    return world.mods_dir / MOD_ID


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_copies_the_mod_content_and_nothing_else(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None

    result = install_mod(world.mod_source, world.mods_dir)

    names = set(_tree(result.destination))
    assert "mod.info" in names
    assert "media/lua/shared/PZAgent/Json.lua" in names
    # The source carries a .bin file; the extension allowlist keeps it out.
    assert "notes.bin" not in names
    assert result.files_written == 3


def test_the_manifest_records_a_hash_per_file(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None

    result = install_mod(world.mod_source, world.mods_dir)

    manifest = read_manifest(result.destination)
    assert manifest is not None
    assert manifest.mod_id == MOD_ID
    assert manifest.mod_version == MOD_VERSION
    assert manifest.product_version == PRODUCT_VERSION
    assert {entry.path for entry in manifest.files} == set(_tree(result.destination)) - {
        MANIFEST_NAME
    }
    assert all(len(entry.sha256) == 64 for entry in manifest.files)


def test_the_game_installation_is_never_touched(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    assert world.install_dir is not None
    before = _tree(world.install_dir)

    install_mod(world.mod_source, world.mods_dir)

    assert _tree(world.install_dir) == before


def test_installing_twice_is_idempotent_and_reports_what_it_replaced(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    first = install_mod(world.mod_source, world.mods_dir)
    before = _tree(first.destination)
    del before[MANIFEST_NAME]

    second = install_mod(world.mod_source, world.mods_dir)

    after = _tree(second.destination)
    # The ledger legitimately differs: it records when the install happened.
    assert after.pop(MANIFEST_NAME) is not None
    assert after == before
    assert second.manifest.files == first.manifest.files
    assert len(second.replaced) == second.files_written


def test_a_file_from_an_older_version_is_removed_on_reinstall(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    old_source = make_mod_source(tmp_path / "old")
    (old_source / "media" / "lua" / "shared" / "PZAgent" / "Gone.lua").write_text(
        "-- removed in the next version\n", encoding="utf-8"
    )
    install_mod(old_source, world.mods_dir)
    assert world.mod_source is not None

    result = install_mod(world.mod_source, world.mods_dir)

    assert result.removed_stale == ("media/lua/shared/PZAgent/Gone.lua",)
    assert not (result.destination / "media/lua/shared/PZAgent/Gone.lua").exists()


def test_an_older_version_file_the_user_edited_is_kept_rather_than_deleted(
    tmp_path: Path,
) -> None:
    """Dropping a file from a release is not authority to delete the user's copy.

    Uninstall already keeps a modified file; install removed the same file
    without looking at its hash, so upgrading silently discarded an edit that
    uninstalling would have preserved.
    """
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    old_source = make_mod_source(tmp_path / "old")
    gone = old_source / "media" / "lua" / "shared" / "PZAgent" / "Gone.lua"
    gone.write_text("-- removed in the next version\n", encoding="utf-8")
    installed = install_mod(old_source, world.mods_dir)
    edited = installed.destination / "media" / "lua" / "shared" / "PZAgent" / "Gone.lua"
    edited.write_text("-- my own tweak\n", encoding="utf-8")

    result = install_mod(world.mod_source, world.mods_dir)

    assert result.kept_stale == ("media/lua/shared/PZAgent/Gone.lua",)
    assert result.removed_stale == ()
    assert edited.read_text(encoding="utf-8") == "-- my own tweak\n"


def test_a_stale_file_already_gone_is_not_reported_as_removed(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    old_source = make_mod_source(tmp_path / "old")
    (old_source / "media" / "lua" / "shared" / "PZAgent" / "Gone.lua").write_text(
        "-- removed in the next version\n", encoding="utf-8"
    )
    installed = install_mod(old_source, world.mods_dir)
    (installed.destination / "media" / "lua" / "shared" / "PZAgent" / "Gone.lua").unlink()

    result = install_mod(world.mod_source, world.mods_dir)

    assert result.removed_stale == ()
    assert result.kept_stale == ()


def test_a_copy_that_fails_part_way_records_what_landed_so_a_retry_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial install with no ledger would be refused as foreign for ever."""
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    real = shutil.copyfile
    seen = {"calls": 0}

    def flaky(source: Any, destination: Any, **kwargs: Any) -> Any:
        seen["calls"] += 1
        if seen["calls"] == 2:
            raise OSError(28, "No space left on device")
        return real(source, destination, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", flaky)

    with pytest.raises(InstallError, match="copy failed after 1 file"):
        install_mod(world.mod_source, world.mods_dir)

    partial = read_manifest(_installed(world))
    assert partial is not None
    assert len(partial.files) == 1
    monkeypatch.undo()

    retried = install_mod(world.mod_source, world.mods_dir)

    assert retried.files_written == 3


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_a_source_with_more_files_than_the_cap_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    source = make_mod_source(tmp_path / "wide")
    for index in range(MAX_INSTALL_FILES + 1):
        (source / "media" / "lua" / "client" / f"F{index}.lua").write_text("-- x\n")

    with pytest.raises(InstallError, match=f"more than {MAX_INSTALL_FILES} files"):
        install_mod(source, world.mods_dir)

    assert not _installed(world).exists()


def test_a_file_larger_than_the_per_file_cap_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    source = make_mod_source(tmp_path / "heavy")
    huge = source / "media" / "lua" / "client" / "Huge.lua"
    with huge.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)

    with pytest.raises(InstallError, match=f"exceeds the {MAX_FILE_BYTES} cap"):
        install_mod(source, world.mods_dir)

    assert not _installed(world).exists()


def test_a_path_deeper_than_the_cap_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    source = make_mod_source(tmp_path / "deep")
    deep = source.joinpath(*[f"d{index}" for index in range(MAX_DEPTH)])
    deep.mkdir(parents=True)
    (deep / "Deep.lua").write_text("-- x\n", encoding="utf-8")

    with pytest.raises(InstallError, match="deeper than"):
        install_mod(source, world.mods_dir)


def test_a_source_that_is_not_a_mod_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    empty = tmp_path / "not-a-mod"
    empty.mkdir()

    with pytest.raises(InstallError, match=r"no mod\.info"):
        install_mod(empty, world.mods_dir)


# ---------------------------------------------------------------------------
# refusing to overwrite
# ---------------------------------------------------------------------------


def test_a_foreign_file_stops_the_install_before_anything_is_written(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    destination = _installed(world)
    (destination / "media" / "lua" / "shared" / "PZAgent").mkdir(parents=True)
    foreign = destination / "media" / "lua" / "shared" / "PZAgent" / "Json.lua"
    foreign.write_text("-- somebody else's file\n", encoding="utf-8")

    with pytest.raises(ForeignFileError) as caught:
        install_mod(world.mod_source, world.mods_dir)

    assert caught.value.path == foreign
    assert "did not install" in caught.value.reason
    assert foreign.read_text(encoding="utf-8") == "-- somebody else's file\n"
    assert not (destination / MANIFEST_NAME).exists()


def test_a_file_edited_after_install_stops_a_reinstall(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    result = install_mod(world.mod_source, world.mods_dir)
    edited = result.destination / "media" / "lua" / "shared" / "PZAgent" / "Json.lua"
    edited.write_text("-- my own tweak\n", encoding="utf-8")

    with pytest.raises(ForeignFileError, match="modified since install"):
        install_mod(world.mod_source, world.mods_dir)

    assert edited.read_text(encoding="utf-8") == "-- my own tweak\n"


def test_an_unreadable_manifest_is_not_treated_as_a_fresh_install(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    destination = _installed(world)
    destination.mkdir(parents=True)
    (destination / MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallError, match="present but unreadable"):
        install_mod(world.mod_source, world.mods_dir)


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_the_round_trip_leaves_no_residue(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    sibling = world.mods_dir / "SomeoneElsesMod"
    (sibling / "media").mkdir(parents=True)
    (sibling / "mod.info").write_text("id=other\n", encoding="utf-8")
    before = _tree(world.mods_dir)

    install_mod(world.mod_source, world.mods_dir)
    result = uninstall_mod(world.mods_dir)

    assert result.directory_removed
    assert not _installed(world).exists()
    assert _tree(world.mods_dir) == before


def test_uninstall_keeps_a_file_the_user_edited_and_says_so(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    result = install_mod(world.mod_source, world.mods_dir)
    edited = result.destination / "media" / "lua" / "shared" / "PZAgent" / "Json.lua"
    edited.write_text("-- my own tweak\n", encoding="utf-8")

    outcome = uninstall_mod(world.mods_dir)

    assert outcome.kept_modified == ("media/lua/shared/PZAgent/Json.lua",)
    assert edited.exists()
    assert not outcome.directory_removed


def test_uninstall_removes_only_what_the_manifest_records(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    result = install_mod(world.mod_source, world.mods_dir)
    stray = result.destination / "media" / "notes-of-my-own.txt"
    stray.write_text("mine\n", encoding="utf-8")

    outcome = uninstall_mod(world.mods_dir)

    assert stray.exists()
    assert "media/notes-of-my-own.txt" not in outcome.removed
    assert not outcome.directory_removed


def test_a_file_already_gone_is_reported_rather_than_failing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None and world.mod_source is not None
    result = install_mod(world.mod_source, world.mods_dir)
    (result.destination / "mod.info").unlink()

    outcome = uninstall_mod(world.mods_dir)

    assert outcome.missing == ("mod.info",)
    assert outcome.directory_removed


def test_a_directory_with_no_manifest_is_never_deleted(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    destination = _installed(world)
    destination.mkdir(parents=True)
    (destination / "mod.info").write_text("id=pz_agent_bridge\n", encoding="utf-8")

    with pytest.raises(InstallError, match="no record of installing"):
        uninstall_mod(world.mods_dir)

    assert (destination / "mod.info").exists()


def test_uninstalling_what_is_not_installed_says_so(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None

    with pytest.raises(InstallError, match="is not installed"):
        uninstall_mod(world.mods_dir)


# ---------------------------------------------------------------------------
# mod.info and source discovery
# ---------------------------------------------------------------------------


def test_mod_info_is_parsed_as_the_key_value_file_the_game_reads(tmp_path: Path) -> None:
    source = make_mod_source(tmp_path, version="0.2.0")

    fields, problem = read_mod_info(source)

    assert problem == ""
    assert fields is not None
    assert fields["id"] == MOD_ID
    assert fields["modversion"] == "0.2.0"


def test_mod_info_without_an_id_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "mod.info").write_text("name=Something\n", encoding="utf-8")

    fields, problem = read_mod_info(directory)

    assert fields is None
    assert "no id= line" in problem


def test_the_repository_mod_source_is_found_from_this_package() -> None:
    """The default source is the checkout's own pz-mod/42."""
    found = find_source()

    assert found is not None
    assert (found / "mod.info").is_file()
    assert found.name == "42"


# ---------------------------------------------------------------------------
# through the CLI
# ---------------------------------------------------------------------------


def test_the_cli_round_trip_reports_both_halves(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("install-mod") == EXIT_OK
    assert "Enable 'PZ Agent Bridge'" in world.stdout
    world.reset_streams()
    assert world.run("uninstall-mod") == EXIT_OK
    assert "Saves, backups and configuration were not touched." in world.stdout
    # The exchange directory is the mod's, not the installer's: it is named
    # rather than deleted, so the user is told what is left instead of guessing.
    assert "written by the mod, not by install-mod" in world.stdout


def test_the_cli_refuses_a_foreign_file_and_names_it_redacted(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    destination = _installed(world)
    (destination / "media" / "lua" / "shared" / "PZAgent").mkdir(parents=True)
    (destination / "media" / "lua" / "shared" / "PZAgent" / "Json.lua").write_text(
        "-- theirs\n", encoding="utf-8"
    )

    exit_code = world.run("install-mod")

    assert exit_code == EXIT_FAILURE
    assert "Nothing was written" in world.stderr
    assert "<ZOMBOID>" in world.stderr
    assert world.home.name not in world.stderr


def test_the_cli_json_output_lists_the_manifest(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("install-mod", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert document["files_written"] == 3
    assert document["manifest"]["mod_id"] == MOD_ID
    assert document["destination"].startswith("<ZOMBOID>")


def test_install_without_a_zomboid_directory_explains_where_to_look(tmp_path: Path) -> None:
    world = make_world(tmp_path, with_user_dir=False)

    assert world.run("install-mod") == EXIT_FAILURE
    assert "PZD003" in world.stderr
