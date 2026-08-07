"""The Windows packaging story, driven on Linux by injecting the target root.

The installer's failure modes are all environmental — a profile that is not where
the environment says it is, somebody else's mod already in the mods folder, a
config the user edited — so every one of them is built here against ``tmp_path``
rather than described.
"""

from __future__ import annotations

import io
import json
import re
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
from pz_agent_core.platform.paths import portable_relative_path
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
    """Every path under *root*, named the way the manifest names one.

    Portable, not native. ``str(relative_to(...))`` gives ``pz-agent\\config.toml``
    on Windows and ``pz-agent/config.toml`` elsewhere, so a comparison against a
    manifest entry — which is portable by contract — failed on Windows only.
    """
    return {portable_relative_path(path, root) for path in sorted(root.rglob("*"))}


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
    # Written as bytes, and compared as bytes. The point of this test is that
    # the installer does not touch a configuration the user wrote, and "does not
    # touch" is a statement about bytes: writing it as text here would have the
    # test itself translate the newlines on Windows, so it measured the harness
    # rather than the installer, and `len(body)` disagreed with the file by two.
    body = b'[session]\ndefault_mode = "assisted"\n'
    config.write_bytes(body)

    result = install(target, _payload(tmp_path))

    assert config.read_bytes() == body, "the installer rewrote a config it should have kept"
    assert result.config_created is False
    recorded = result.manifest.by_path()[f"{STATE_DIR_NAME}/{CONFIG_NAME}"]
    assert recorded.preserved is True
    assert recorded.size == config.stat().st_size
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


def test_every_manifest_path_is_portable_rather_than_native(tmp_path: Path) -> None:
    """The manifest is read back by the uninstaller, possibly by another build.

    `str(relative_to(...))` gives `pz-agent\\config.toml` on Windows and
    `pz-agent/config.toml` elsewhere. A manifest written by one and read by the
    other has no entry that matches, so an uninstall would report every file as
    already gone and remove nothing — a silent no-op, not an error.
    """
    target = _target(tmp_path)

    result = install(target, _payload(tmp_path))
    ledger = json.loads((target / STATE_DIR_NAME / MANIFEST_NAME).read_text(encoding="utf-8"))

    recorded = [entry["path"] for entry in ledger["files"]] + list(ledger["directories"])
    assert recorded, "the manifest recorded nothing to check"
    for path in recorded:
        assert "\\" not in path, f"native separator in the manifest: {path!r}"
        assert not path.startswith("/"), f"manifest path is not relative: {path!r}"
        assert ":" not in path, f"manifest path names a drive: {path!r}"
    assert [entry.path for entry in result.manifest.files] == [
        entry["path"] for entry in ledger["files"]
    ]


def test_the_manifest_does_not_depend_on_where_it_was_installed(tmp_path: Path) -> None:
    """Two targets, the same ledger — which is what "portable" has to mean.

    A path that leaked the install root would differ between these two, and
    would carry the account name into a file the user is told they can attach
    to a bug report.
    """
    first = _target(tmp_path / "one")
    second = _target(tmp_path / "two" / "Игры")

    left = install(first, _payload(tmp_path))
    right = install(second, _payload(tmp_path))

    assert [entry.path for entry in left.manifest.files] == [
        entry.path for entry in right.manifest.files
    ]
    assert left.manifest.directories == right.manifest.directories
    assert CYRILLIC_USER not in json.dumps(
        [entry.to_dict() for entry in left.manifest.files], ensure_ascii=False
    )


def test_the_installer_and_the_cli_agree_on_the_state_directory_name() -> None:
    assert STATE_DIR_NAME == CLI_STATE_DIR_NAME


def test_the_generated_config_takes_its_build_from_the_payload() -> None:
    assert 'expected_build = "42.20"' in render_config(expected_build="42.20")
    assert 'expected_build = "42.21"' in render_config(expected_build="42.21")


def test_the_launcher_starts_the_sidecar_before_the_game_and_names_the_config() -> None:
    # The path is asserted as the OS spells it rather than as a POSIX literal:
    # a launcher is read by cmd.exe and must carry a native path, and comparing
    # against a hardcoded "/x/..." failed on Windows for being right.
    config_path = Path("/x/Zomboid/pz-agent/config.toml")
    text = render_launcher(config_path=config_path)

    start_at = text.index("pz-agent --config")
    steam_at = text.index("steam://rungameid/108600")
    assert start_at < steam_at
    assert str(config_path) in text
    assert "pz-agent arm" in text
    assert text.endswith("endlocal\r\n")


def test_the_launcher_sets_a_utf8_codepage_before_any_non_ascii_path() -> None:
    """A Cyrillic profile is the case this project was built for.

    cmd.exe reads a .bat in the console's OEM codepage — 866 or 1251 on a
    Russian Windows — and the installer writes UTF-8. Without `chcp 65001` the
    path is read as mojibake and the sidecar starts against a directory that
    does not exist, which looks like a broken install rather than an encoding.
    """
    text = render_launcher(config_path=Path("C:/Users/Иван/Zomboid/pz-agent/config.toml"))

    codepage_at = text.index("chcp 65001")
    assert codepage_at < text.index("Иван"), "the codepage is set after the path it has to decode"
    assert ">nul" in text[codepage_at : codepage_at + 24], (
        "the codepage number is printed at startup"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "C:/Users/Иван/Zomboid/pz-agent/config.toml",
        "C:/Users/John Smith/Zomboid/pz-agent/config.toml",
        "C:/Program Files/Zomboid/pz-agent/config.toml",
    ],
    ids=["cyrillic", "spaces", "program-files"],
)
def test_every_expansion_of_the_config_path_is_quoted(raw: str) -> None:
    """A path with a space in it is the ordinary case, not the exotic one."""
    text = render_launcher(config_path=Path(raw))

    assert f'set "PZ_AGENT_CONFIG={Path(raw)}"' in text
    for line in text.splitlines():
        if "%PZ_AGENT_CONFIG%" in line:
            assert '"%PZ_AGENT_CONFIG%"' in line, f"unquoted expansion: {line}"


def _unquoted_spans(line: str) -> list[str]:
    """The parts of a batch line that are *not* inside double quotes.

    cmd.exe has no other quoting: a path is either between two ``"`` or it is
    split on every space it contains. Splitting on ``"`` and taking the even
    elements is therefore an exact model of what the shell will see bare.
    """
    return line.split('"')[0::2]


#: A drive-qualified path or a URL — anything with a separator right after a
#: colon. `with:` and `errorlevel 1` are not paths and must not be flagged.
_PATHISH = re.compile(r"[A-Za-z]:[\\/]|://")

#: A variable expansion. `%PZ_AGENT_CONFIG%` holds a path; bare, it is split on
#: every space in that path, which is the failure `C:\Users\John Smith` shows.
_EXPANSION = re.compile(r"%[~\w]+%|%~\d")


@pytest.mark.parametrize(
    "raw",
    [
        "C:/Users/Иван/Zomboid/pz-agent/config.toml",
        "C:/Users/John Smith/Zomboid/pz-agent/config.toml",
        "C:/Program Files/Zomboid/pz-agent/config.toml",
        "C:/Users/Иван Петров/Zomboid/pz-agent/config.toml",
    ],
    ids=["cyrillic", "spaces", "program-files", "cyrillic-with-space"],
)
def test_no_path_in_the_launcher_is_left_for_cmd_to_split_on_a_space(raw: str) -> None:
    """Every path in the whole file, not only the ones spelled `%PZ_AGENT_CONFIG%`.

    The narrower test above checks the expansions it knows the name of, which
    cannot notice a path added later under a different name. This one models
    cmd.exe's quoting and asserts over the rendered file, so a new unquoted path
    fails it whatever it is called.
    """
    text = render_launcher(config_path=Path(raw))

    for number, line in enumerate(text.splitlines(), start=1):
        assert line.count('"') % 2 == 0, f"line {number} has an unbalanced quote: {line}"
        for span in _unquoted_spans(line):
            assert not _PATHISH.search(span), f"line {number} has a bare path: {line}"
            assert not _EXPANSION.search(span), f"line {number} has a bare expansion: {line}"


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
