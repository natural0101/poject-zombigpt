"""The Windows packaging story, driven on Linux by injecting the target root.

The installer's failure modes are all environmental — a profile that is not where
the environment says it is, somebody else's mod already in the mods folder, a
config the user edited — so every one of them is built here against ``tmp_path``
rather than described.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from installer.pz_agent_installer import (
    CONFIG_NAME,
    EXIT_FAILURE,
    EXIT_OK,
    LAUNCHER_NAME,
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    MODS_DIR_NAME,
    STATE_DIR_NAME,
    ForeignFileError,
    InstallerError,
    InstallManifest,
    find_payload,
    find_zomboid_dir,
    install,
    main,
    read_manifest,
    read_mod_info,
    render_config,
    render_launcher,
    uninstall,
)

from pz_agent_cli.config import load_config
from pz_agent_cli.context import STATE_DIR_NAME as CLI_STATE_DIR_NAME
from pz_agent_cli.modinstall import MOD_ID
from tests.fixtures.cli_worlds import make_mod_source
from tests.fixtures.platform_trees import CYRILLIC_USER, make_user_dir

SAVE_FILES = {"map_t.bin": b"tiles", "players.db": b"state"}


def _target(tmp_path: Path) -> Path:
    """A Zomboid directory with a save and a log already in it."""
    home = tmp_path / "drive" / "Users" / CYRILLIC_USER
    home.mkdir(parents=True, exist_ok=True)
    target = make_user_dir(home)
    save = target / "Saves" / "Survivor" / "09-07-1993"
    save.mkdir(parents=True, exist_ok=True)
    for name, body in SAVE_FILES.items():
        (save / name).write_bytes(body)
    (target / "console.txt").write_text("versionNumber=42.20.0\n", encoding="utf-8")
    return target


def _payload(tmp_path: Path) -> Path:
    return make_mod_source(tmp_path / "release")


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in sorted(root.rglob("*"))}


# ---------------------------------------------------------------------------
# install / uninstall round trip
# ---------------------------------------------------------------------------


def test_install_places_the_mod_the_config_the_launcher_and_a_manifest(tmp_path: Path) -> None:
    target = _target(tmp_path)

    result = install(target, _payload(tmp_path))

    assert (target / MODS_DIR_NAME / MOD_ID / "mod.info").is_file()
    assert (target / STATE_DIR_NAME / CONFIG_NAME).is_file()
    assert (target / STATE_DIR_NAME / LAUNCHER_NAME).is_file()
    assert (target / STATE_DIR_NAME / MANIFEST_NAME).is_file()
    assert result.config_created is True
    assert result.manifest.format == MANIFEST_FORMAT
    assert result.manifest.mod_id == MOD_ID
    assert result.files_written == len(result.manifest.files)


def test_the_round_trip_leaves_no_residue_but_the_config(tmp_path: Path) -> None:
    target = _target(tmp_path)
    before = _tree(target)

    install(target, _payload(tmp_path))
    uninstall(target)

    after = _tree(target)
    added = after - before
    assert added == {STATE_DIR_NAME, f"{STATE_DIR_NAME}/{CONFIG_NAME}"}
    assert not (target / MODS_DIR_NAME / MOD_ID).exists()


def test_uninstall_reports_the_config_as_kept_rather_than_removed(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))

    result = uninstall(target)

    assert result.preserved == (f"{STATE_DIR_NAME}/{CONFIG_NAME}",)
    assert f"{STATE_DIR_NAME}/{CONFIG_NAME}" not in result.removed
    assert (target / STATE_DIR_NAME / CONFIG_NAME).is_file()


def test_uninstall_leaves_saves_alone(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))

    uninstall(target)

    save = target / "Saves" / "Survivor" / "09-07-1993"
    assert {path.name: path.read_bytes() for path in sorted(save.iterdir())} == SAVE_FILES


def test_uninstall_leaves_backups_and_logs_alone(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))
    backups = target / STATE_DIR_NAME / "backups" / "2026-08-05"
    backups.mkdir(parents=True)
    (backups / "manifest.json").write_text("{}", encoding="utf-8")
    logs = target / STATE_DIR_NAME / "logs"
    logs.mkdir()
    (logs / "pz-agent.log").write_text("tick\n", encoding="utf-8")

    uninstall(target)

    assert (backups / "manifest.json").is_file()
    assert (logs / "pz-agent.log").read_text(encoding="utf-8") == "tick\n"


def test_uninstall_keeps_a_mod_file_the_user_edited_and_names_it(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))
    edited = target / MODS_DIR_NAME / MOD_ID / "media" / "lua" / "shared" / "PZAgent" / "Json.lua"
    edited.write_text("-- mine now\n", encoding="utf-8")

    result = uninstall(target)

    assert edited.is_file()
    assert any(path.endswith("Json.lua") for path in result.kept_modified)
    assert not any(path.endswith("Json.lua") for path in result.removed)


def test_uninstall_reports_a_file_that_was_already_gone_without_failing(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))
    (target / MODS_DIR_NAME / MOD_ID / "mod.info").unlink()

    result = uninstall(target)

    assert result.missing == ("mods/pz_agent_bridge/mod.info",)


def test_uninstall_refuses_when_there_is_no_manifest_to_consult(tmp_path: Path) -> None:
    target = _target(tmp_path)
    (target / MODS_DIR_NAME / MOD_ID).mkdir(parents=True)

    with pytest.raises(InstallerError, match=r"no record of installing anything here"):
        uninstall(target)

    assert (target / MODS_DIR_NAME / MOD_ID).is_dir()


def test_a_manifest_in_an_unknown_format_is_refused_rather_than_half_read(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))
    ledger = target / STATE_DIR_NAME / MANIFEST_NAME
    document = json.loads(ledger.read_text(encoding="utf-8"))
    document["format"] = "pz-agent-installer/99"
    ledger.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InstallerError, match=r"will not remove files it cannot account for"):
        uninstall(target)


# ---------------------------------------------------------------------------
# refusing to overwrite
# ---------------------------------------------------------------------------


def test_install_refuses_a_file_it_did_not_write_and_writes_nothing(tmp_path: Path) -> None:
    target = _target(tmp_path)
    squatter = target / MODS_DIR_NAME / MOD_ID / "mod.info"
    squatter.parent.mkdir(parents=True)
    squatter.write_text("name=Somebody else's mod\nid=pz_agent_bridge\n", encoding="utf-8")

    with pytest.raises(ForeignFileError) as caught:
        install(target, _payload(tmp_path))

    assert caught.value.path == squatter
    assert "did not write" in caught.value.reason
    assert squatter.read_text(encoding="utf-8").startswith("name=Somebody else's mod")
    assert not (target / STATE_DIR_NAME / LAUNCHER_NAME).exists()
    assert read_manifest(target) is None


def test_install_refuses_a_file_of_its_own_that_has_since_been_edited(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))
    edited = target / MODS_DIR_NAME / MOD_ID / "mod.info"
    edited.write_text("name=edited\nid=pz_agent_bridge\n", encoding="utf-8")

    with pytest.raises(ForeignFileError, match=r"modified since it was installed"):
        install(target, _payload(tmp_path))

    assert edited.read_text(encoding="utf-8") == "name=edited\nid=pz_agent_bridge\n"


def test_reinstalling_over_its_own_untouched_files_is_allowed(tmp_path: Path) -> None:
    target = _target(tmp_path)
    install(target, _payload(tmp_path))

    result = install(target, _payload(tmp_path))

    assert "mods/pz_agent_bridge/mod.info" in result.replaced
    assert result.config_created is False


def test_an_existing_config_is_left_exactly_as_the_user_wrote_it(tmp_path: Path) -> None:
    target = _target(tmp_path)
    config = target / STATE_DIR_NAME / CONFIG_NAME
    config.parent.mkdir(parents=True)
    body = '[session]\ndefault_mode = "assisted"\n'
    config.write_text(body, encoding="utf-8")

    result = install(target, _payload(tmp_path))

    assert config.read_text(encoding="utf-8") == body
    assert result.config_created is False
    recorded = result.manifest.by_path()[f"{STATE_DIR_NAME}/{CONFIG_NAME}"]
    assert recorded.preserved is True
    assert recorded.size == len(body)


def test_an_unreadable_manifest_stops_the_install_rather_than_authorising_it(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    ledger = target / STATE_DIR_NAME / MANIFEST_NAME
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallerError, match=r"present but unreadable"):
        install(target, _payload(tmp_path))


# ---------------------------------------------------------------------------
# bounds and untrusted input
# ---------------------------------------------------------------------------


def test_only_mod_shaped_files_are_copied(tmp_path: Path) -> None:
    target = _target(tmp_path)
    payload = _payload(tmp_path)

    result = install(target, payload)

    assert (payload / "notes.bin").is_file()
    assert not (target / MODS_DIR_NAME / MOD_ID / "notes.bin").exists()
    assert all(not entry.path.endswith(".bin") for entry in result.manifest.files)


def test_a_payload_file_over_the_per_file_cap_is_refused(tmp_path: Path) -> None:
    target = _target(tmp_path)
    payload = _payload(tmp_path)
    (payload / "huge.txt").write_bytes(b"x" * (4 * 1024 * 1024 + 1))

    with pytest.raises(InstallerError, match=r"exceeds the \d+ byte per-file cap"):
        install(target, payload)


def test_a_payload_with_no_mod_info_is_not_a_mod(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-mod"
    empty.mkdir()

    with pytest.raises(InstallerError, match=r"cannot read mod.info"):
        install(_target(tmp_path), empty)


def test_a_mod_info_without_an_id_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "nameless"
    payload.mkdir()
    (payload / "mod.info").write_text("name=Nameless\n", encoding="utf-8")

    with pytest.raises(InstallerError, match=r"no id= line"):
        read_mod_info(payload)


@pytest.mark.parametrize(
    "traversal",
    ["/etc/passwd", "..\\evil.lua", "a/../../b", "\\\\server\\share", "C:/Windows/system32/x"],
)
def test_a_manifest_path_that_could_escape_the_target_is_refused(
    tmp_path: Path, traversal: str
) -> None:
    """The manifest is a file on disk; anything could have edited it."""
    document = {
        "format": MANIFEST_FORMAT,
        "mod_id": MOD_ID,
        "mod_version": "0.1.0",
        "pz_version": "42.20",
        "installed_at": "2026-08-05T12:00:00+00:00",
        "directories": [],
        "files": [
            {"path": traversal, "role": "mod", "size": 1, "sha256": "0" * 64, "preserved": False}
        ],
    }

    with pytest.raises(
        InstallerError,
        match=r"must (not traverse|be relative|not use a backslash|not name a drive)",
    ):
        InstallManifest.from_dict(document)


def test_install_never_touches_the_game_installation(tmp_path: Path) -> None:
    target = _target(tmp_path)
    game = tmp_path / "drive" / "Program Files" / "Steam" / "ProjectZomboid"
    (game / "media").mkdir(parents=True)
    (game / "media" / "lua.txt").write_text("engine", encoding="utf-8")
    before = _tree(game)

    install(target, _payload(tmp_path))

    assert _tree(game) == before


# ---------------------------------------------------------------------------
# what it generates
# ---------------------------------------------------------------------------


def test_the_generated_config_passes_the_validator_the_program_will_run(
    tmp_path: Path,
) -> None:
    """An installer that writes a file pz-agent then refuses is worse than one that writes none."""
    target = _target(tmp_path)
    install(target, _payload(tmp_path))

    validation = load_config(target / STATE_DIR_NAME / CONFIG_NAME)

    assert validation.errors == ()
    assert validation.config is not None
    assert validation.config.default_mode.value == "OBSERVE"


def test_the_installer_and_the_cli_agree_on_the_state_directory_name() -> None:
    assert STATE_DIR_NAME == CLI_STATE_DIR_NAME


def test_the_generated_config_takes_its_build_from_the_payload() -> None:
    assert 'expected_build = "42.20"' in render_config(expected_build="42.20")
    assert 'expected_build = "42.21"' in render_config(expected_build="42.21")


def test_the_launcher_starts_the_sidecar_before_the_game_and_names_the_config() -> None:
    text = render_launcher(config_path=Path("/x/Zomboid/pz-agent/config.toml"))

    start_at = text.index("pz-agent --config")
    steam_at = text.index("steam://rungameid/108600")
    assert start_at < steam_at
    assert "/x/Zomboid/pz-agent/config.toml" in text
    assert "pz-agent arm" in text
    assert text.endswith("endlocal\r\n")


def test_the_launcher_never_arms_anything_by_itself() -> None:
    text = render_launcher(config_path=Path("C:/Zomboid/pz-agent/config.toml"))

    assert 'pz-agent --config "%PZ_AGENT_CONFIG%" arm' not in text
    assert "OBSERVE" in text


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_the_profile_is_found_through_a_cyrillic_user_name(tmp_path: Path) -> None:
    target = _target(tmp_path)
    env: Mapping[str, str] = {"USERPROFILE": str(target.parent), "USERNAME": CYRILLIC_USER}

    found, searched = find_zomboid_dir(root=tmp_path / "drive", env=env)

    assert found == target
    assert searched[0] == str(target)


def test_a_onedrive_relocated_profile_is_found(tmp_path: Path) -> None:
    home = tmp_path / "drive" / "Users" / CYRILLIC_USER
    relocated = make_user_dir(home / "OneDrive")
    env: Mapping[str, str] = {"USERPROFILE": str(home)}

    found, _ = find_zomboid_dir(root=tmp_path / "drive", env=env)

    assert found == relocated


def test_nothing_is_created_when_the_profile_does_not_exist(tmp_path: Path) -> None:
    """An empty Zomboid directory this tool made looks exactly like a real profile."""
    home = tmp_path / "drive" / "Users" / CYRILLIC_USER
    home.mkdir(parents=True)
    env: Mapping[str, str] = {"USERPROFILE": str(home)}

    found, searched = find_zomboid_dir(root=tmp_path / "drive", env=env)

    assert found is None
    assert searched != ()
    assert not (home / "Zomboid").exists()


def test_an_override_that_is_not_a_directory_is_not_silently_replaced(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"

    found, searched = find_zomboid_dir(root=tmp_path, env={}, override=missing)

    assert found is None
    assert searched == (str(missing),)


def test_the_payload_is_found_beside_the_installer_in_this_checkout() -> None:
    payload = find_payload()

    assert payload is not None
    assert (payload / "mod.info").is_file()


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, env: Mapping[str, str], root: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err, env=env, root=root)
    return code, out.getvalue(), err.getvalue()


def test_the_command_line_installs_and_uninstalls(tmp_path: Path) -> None:
    target = _target(tmp_path)
    env: Mapping[str, str] = {"USERPROFILE": str(target.parent)}
    payload = _payload(tmp_path)

    code, out, _ = _run(["install", "--mod-source", str(payload)], env=env, root=tmp_path / "drive")
    assert code == EXIT_OK
    assert "pz-agent arm" in out
    assert (target / MODS_DIR_NAME / MOD_ID / "mod.info").is_file()

    code, out, _ = _run(["uninstall"], env=env, root=tmp_path / "drive")
    assert code == EXIT_OK
    assert "Saves, backups and logs were not touched." in out


def test_the_command_line_names_where_it_looked_when_there_is_no_profile(
    tmp_path: Path,
) -> None:
    home = tmp_path / "drive" / "Users" / CYRILLIC_USER
    home.mkdir(parents=True)

    code, _, err = _run(["install"], env={"USERPROFILE": str(home)}, root=tmp_path / "drive")

    assert code == EXIT_FAILURE
    assert "No Zomboid directory was found" in err
    assert str(home / "Zomboid") in err


def test_the_command_line_reports_a_refusal_without_a_traceback(tmp_path: Path) -> None:
    target = _target(tmp_path)
    squatter = target / MODS_DIR_NAME / MOD_ID / "mod.info"
    squatter.parent.mkdir(parents=True)
    squatter.write_text("id=pz_agent_bridge\n", encoding="utf-8")

    code, _, err = _run(
        ["install", "--mod-source", str(_payload(tmp_path))],
        env={"USERPROFILE": str(target.parent)},
        root=tmp_path / "drive",
    )

    assert code == EXIT_FAILURE
    assert "Refusing to install" in err
    assert "Nothing was written" in err


def test_uninstalling_what_was_never_installed_reports_it(tmp_path: Path) -> None:
    target = _target(tmp_path)

    code, _, err = _run(
        ["uninstall"], env={"USERPROFILE": str(target.parent)}, root=tmp_path / "drive"
    )

    assert code == EXIT_FAILURE
    assert "no record of installing anything here" in err
