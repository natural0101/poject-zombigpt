"""Install and user-directory detection, and the VDF parser underneath it."""

from __future__ import annotations

from pathlib import Path

import pytest

from pz_agent_core.platform.discovery import (
    MAX_LIBRARIES,
    MAX_METADATA_BYTES,
    MAX_PROBLEMS,
    BuildInfo,
    DiscoveryContext,
    DiscoveryError,
    detect_build,
    discover,
    find_install,
    find_steam_libraries,
    find_user_directory,
    steam_root_candidates,
)
from pz_agent_core.platform.vdf import (
    MAX_VDF_DEPTH,
    VdfError,
    as_object,
    as_text,
    get_ci,
    parse_vdf,
)
from pz_agent_core.version import SUPPORTED_BUILDS
from tests.fixtures.platform_trees import (
    CYRILLIC_USER,
    install_game,
    make_library,
    make_steam_root,
    make_user_dir,
    write_console_log,
    write_legacy_libraryfolders,
    write_modern_libraryfolders,
)

# ---------------------------------------------------------------------------
# VDF parser
# ---------------------------------------------------------------------------


def test_parses_modern_nested_form_with_crlf_and_comments() -> None:
    text = (
        "// Steam writes this file\r\n"
        '"libraryfolders"\r\n'
        "{\r\n"
        '\t"0"\r\n'
        "\t{\r\n"
        '\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\r\n'
        '\t\t"apps" { "108600" "42" }\r\n'
        "\t}\r\n"
        "}\r\n"
    )
    document = parse_vdf(text)
    folders = as_object(document["libraryfolders"])
    assert folders is not None
    entry = as_object(folders["0"])
    assert entry is not None
    assert as_text(entry["path"]) == "C:\\Program Files (x86)\\Steam"
    apps = as_object(entry["apps"])
    assert apps is not None
    assert as_text(apps["108600"]) == "42"


def test_parses_legacy_numeric_leaf_form() -> None:
    text = '"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t"1"\n\t"1"\t"D:\\\\Games"\n}\n'
    document = parse_vdf(text)
    folders = as_object(document["LibraryFolders"])
    assert folders is not None
    assert as_text(folders["1"]) == "D:\\Games"


def test_escape_sequences_and_unquoted_tokens() -> None:
    document = parse_vdf('root { tab "a\\tb" newline "a\\nb" quote "a\\"b" other "a\\qb" }')
    root = as_object(document["root"])
    assert root is not None
    assert as_text(root["tab"]) == "a\tb"
    assert as_text(root["newline"]) == "a\nb"
    assert as_text(root["quote"]) == 'a"b'
    # An unknown escape keeps the escaped character, matching Steam's reader.
    assert as_text(root["other"]) == "aqb"


def test_quoted_brace_is_a_value_not_structure() -> None:
    document = parse_vdf('"a" "{" "b" "}"')
    assert as_text(document["a"]) == "{"
    assert as_text(document["b"]) == "}"


def test_platform_conditional_does_not_shift_following_pairs() -> None:
    document = parse_vdf('"a" "1" [$WIN32]\n"b" "2"\n')
    assert as_text(document["a"]) == "1"
    assert as_text(document["b"]) == "2"


def test_duplicate_key_is_last_wins() -> None:
    assert as_text(parse_vdf('"a" "1" "a" "2"')["a"]) == "2"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ('"a" { "b" "1"', "without a matching '}'"),
        ('"a" { } }', "without a matching '{'"),
        ('"a" "unterminated', "unterminated quoted string"),
        ('"a" { "dangling" }', "has no value"),
        ('"a"', "has no value"),
        ("{ }", "does not follow a key"),
    ],
)
def test_malformed_documents_raise(text: str, fragment: str) -> None:
    with pytest.raises(VdfError) as excinfo:
        parse_vdf(text)
    assert fragment in str(excinfo.value)


def test_depth_is_bounded() -> None:
    deep = '"k" {' * (MAX_VDF_DEPTH + 2) + "}" * (MAX_VDF_DEPTH + 2)
    with pytest.raises(VdfError, match="nesting deeper"):
        parse_vdf(deep)


def test_token_count_is_bounded() -> None:
    with pytest.raises(VdfError, match="more than 4 tokens"):
        parse_vdf('"a" "1" "b" "2" "c" "3"', max_tokens=4)


def test_get_ci_matches_either_casing() -> None:
    document = parse_vdf('"LibraryFolders" { "Path" "x" }')
    folders = as_object(get_ci(document, "libraryfolders"))
    assert folders is not None
    assert as_text(get_ci(folders, "PATH")) == "x"
    assert get_ci(folders, "missing") is None
    assert as_object("leaf") is None
    assert as_text({"a": "b"}) is None


# ---------------------------------------------------------------------------
# Steam library enumeration
# ---------------------------------------------------------------------------


def test_steam_root_candidates_prefer_hints_then_env_then_root(tmp_path: Path) -> None:
    hint = tmp_path / "hinted"
    ctx = DiscoveryContext(
        root=tmp_path,
        env={"ProgramFiles(x86)": str(tmp_path / "PF86"), "SteamPath": str(tmp_path / "client")},
        steam_root_hints=(hint,),
    )
    candidates = steam_root_candidates(ctx)
    assert candidates[0] == hint
    assert candidates[1] == tmp_path / "client"
    assert tmp_path / "PF86" / "Steam" in candidates
    assert tmp_path / "Program Files (x86)" / "Steam" in candidates


def test_finds_every_library_from_modern_file(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    second = make_library(tmp_path, "SteamLibrary-D")
    third = make_library(tmp_path, "Games with spaces")
    write_modern_libraryfolders(steam_root, [steam_root, second, third])

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert scan.found
    assert scan.paths == (steam_root, second, third)
    assert scan.problems == ()
    assert any("libraryfolders.vdf" in entry for entry in scan.searched)


def test_finds_every_library_from_legacy_file(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    second = make_library(tmp_path, "Legacy")
    write_legacy_libraryfolders(steam_root, [second])

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    # The Steam root is a library in its own right even though the legacy file
    # only lists the extra ones.
    assert scan.paths == (steam_root, second)


def test_windows_separators_in_vdf_are_understood(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    second = make_library(tmp_path, "OnAnotherDrive")
    write_modern_libraryfolders(steam_root, [second], windows_separators=True)

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert second in scan.paths


def test_cyrillic_library_path_is_enumerated(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    cyrillic = make_library(tmp_path / CYRILLIC_USER, "Игры Steam")
    write_modern_libraryfolders(steam_root, [cyrillic])

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert cyrillic in scan.paths


def test_config_subdirectory_variant_is_read(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    second = make_library(tmp_path, "FromConfig")
    write_modern_libraryfolders(steam_root, [second], subdir="config")

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert second in scan.paths


def test_malformed_vdf_is_reported_without_losing_the_steam_root(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    broken = steam_root / "steamapps" / "libraryfolders.vdf"
    broken.write_text('"libraryfolders"\n{\n\t"0"\n\t{\n', encoding="utf-8")

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert scan.paths == (steam_root,)
    assert any("malformed VDF" in problem for problem in scan.problems)


def test_library_listed_but_absent_is_reported_once(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    missing = tmp_path / "UnpluggedDrive"
    write_modern_libraryfolders(steam_root, [missing])
    write_modern_libraryfolders(steam_root, [missing], subdir="config")

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    dead = [p for p in scan.problems if "no steamapps/" in p]
    assert len(dead) == 1
    assert str(missing) in dead[0]


def test_oversized_vdf_is_refused_not_truncated(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    vdf.write_text("// " + "x" * (1024 * 1024 + 16), encoding="utf-8")

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert any("refusing to parse" in problem for problem in scan.problems)


def test_non_utf8_vdf_is_reported(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    vdf.write_bytes(b'"libraryfolders"\n{\n\t"0"\t"\xff\xfe"\n}\n')

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert any("not valid UTF-8" in problem for problem in scan.problems)


def test_library_count_is_bounded(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    extra = [
        make_library(tmp_path / "many", f"lib{index:03d}") for index in range(MAX_LIBRARIES + 5)
    ]
    write_modern_libraryfolders(steam_root, extra)

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert len(scan.libraries) == MAX_LIBRARIES
    assert any("stopped after" in problem for problem in scan.problems)


def test_problem_list_is_bounded(tmp_path: Path) -> None:
    """One corrupt file can name thousands of dead paths; doctor output cannot.

    The count is still reported, so bounding the list does not turn "37 broken
    entries" into "32 broken entries".
    """
    steam_root = make_steam_root(tmp_path)
    dead = [tmp_path / "gone" / f"lib{index:03d}" for index in range(MAX_PROBLEMS + 5)]
    write_modern_libraryfolders(steam_root, dead)

    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert scan.paths == (steam_root,)
    assert len(scan.problems) == MAX_PROBLEMS + 1
    assert scan.problems[-1] == "... and 5 further problem(s), not listed"


def test_no_steam_at_all_reports_where_it_looked(tmp_path: Path) -> None:
    scan = find_steam_libraries(DiscoveryContext(root=tmp_path))

    assert not scan.found
    assert str(tmp_path / "Program Files (x86)" / "Steam" / "steamapps") in scan.searched
    assert str(tmp_path / "Steam" / "steamapps" / "libraryfolders.vdf") in scan.searched


# ---------------------------------------------------------------------------
# install location
# ---------------------------------------------------------------------------


def test_install_found_in_a_non_default_library(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    second = make_library(tmp_path, "SteamLibrary-D")
    write_modern_libraryfolders(steam_root, [steam_root, second])
    install = install_game(second)

    location = find_install(DiscoveryContext(root=tmp_path))

    assert location.found
    assert location.require() == install
    assert len(location.libraries) == 2


def test_install_with_spaced_directory_name(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    write_modern_libraryfolders(steam_root, [steam_root])
    install = install_game(steam_root, dir_name="Project Zomboid")

    assert find_install(DiscoveryContext(root=tmp_path)).require() == install


def test_leftover_directory_without_media_is_not_an_install(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    write_modern_libraryfolders(steam_root, [steam_root])
    install_game(steam_root, with_media=False)

    location = find_install(DiscoveryContext(root=tmp_path))

    assert not location.found
    assert any("no media/" in problem for problem in location.problems)


def test_missing_install_reports_searched_locations_and_require_raises(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    write_modern_libraryfolders(steam_root, [steam_root])

    location = find_install(DiscoveryContext(root=tmp_path))

    assert not location.found
    assert location.searched
    with pytest.raises(DiscoveryError, match="not found"):
        location.require()


def test_no_library_at_all_is_called_out(tmp_path: Path) -> None:
    location = find_install(DiscoveryContext(root=tmp_path))

    assert not location.found
    assert "no Steam library folder was found" in location.problems


def test_install_override_wins_and_is_validated(tmp_path: Path) -> None:
    manual = tmp_path / "Portable" / "ProjectZomboid"
    (manual / "media").mkdir(parents=True)

    location = find_install(DiscoveryContext(root=tmp_path, install_override=manual))
    assert location.require() == manual

    empty = tmp_path / "Portable" / "Empty"
    empty.mkdir()
    rejected = find_install(DiscoveryContext(root=tmp_path, install_override=empty))
    assert not rejected.found
    assert any("install override" in problem for problem in rejected.problems)


def test_cyrillic_install_path(tmp_path: Path) -> None:
    library = make_library(tmp_path / "Диск D" / CYRILLIC_USER, "Стим Библиотека")
    install = install_game(library)

    location = find_install(DiscoveryContext(root=tmp_path, steam_root_hints=(library,)))

    assert location.require() == install


# ---------------------------------------------------------------------------
# user directory
# ---------------------------------------------------------------------------


def test_user_dir_from_userprofile(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "player one"
    user_dir = make_user_dir(home)

    found = find_user_directory(DiscoveryContext(root=tmp_path, env={"USERPROFILE": str(home)}))

    assert found.found
    assert found.require() == user_dir
    assert found.source == "USERPROFILE"
    assert found.saves_dir == user_dir / "Saves"
    assert found.mods_dir == user_dir / "mods"
    assert found.lua_dir == user_dir / "Lua"


def test_user_dir_with_cyrillic_username(tmp_path: Path) -> None:
    home = tmp_path / "Users" / CYRILLIC_USER
    user_dir = make_user_dir(home)

    found = find_user_directory(DiscoveryContext(root=tmp_path, env={"USERPROFILE": str(home)}))

    assert found.require() == user_dir


def test_custom_home_override_beats_the_environment(tmp_path: Path) -> None:
    env_home = tmp_path / "Users" / "default"
    make_user_dir(env_home)
    portable = tmp_path / "PortableHome"
    portable_dir = make_user_dir(portable)

    found = find_user_directory(
        DiscoveryContext(
            root=tmp_path,
            env={"USERPROFILE": str(env_home)},
            home_override=portable,
        )
    )

    assert found.require() == portable_dir
    assert found.source == "home_override"


def test_cachedir_override_is_used_verbatim(tmp_path: Path) -> None:
    explicit = tmp_path / "D" / "pz-data"
    explicit.mkdir(parents=True)

    found = find_user_directory(DiscoveryContext(root=tmp_path, zomboid_dir_override=explicit))

    assert found.require() == explicit
    assert found.source == "cachedir_override"


def test_onedrive_relocated_profile_is_found(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "player"
    home.mkdir(parents=True)
    relocated = make_user_dir(home / "OneDrive")

    found = find_user_directory(DiscoveryContext(root=tmp_path, env={"USERPROFILE": str(home)}))

    assert found.require() == relocated
    assert found.source == "USERPROFILE_onedrive"


def test_user_dir_falls_back_to_homedrive_and_username(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "fallback"
    user_dir = make_user_dir(home)

    by_username = find_user_directory(DiscoveryContext(root=tmp_path, env={"USERNAME": "fallback"}))
    assert by_username.require() == user_dir
    assert by_username.source == "root_users"

    by_homedrive = find_user_directory(
        DiscoveryContext(
            root=tmp_path,
            env={"HOMEDRIVE": str(tmp_path), "HOMEPATH": "/Users/fallback"},
        )
    )
    assert by_homedrive.require() == user_dir
    assert by_homedrive.source == "HOMEDRIVE+HOMEPATH"


def test_missing_user_dir_lists_candidates_and_require_raises(tmp_path: Path) -> None:
    found = find_user_directory(
        DiscoveryContext(root=tmp_path, env={"USERPROFILE": str(tmp_path / "nobody")})
    )

    assert not found.found
    assert found.source == "none"
    assert str(tmp_path / "nobody" / "Zomboid") in found.searched
    with pytest.raises(DiscoveryError, match="not found"):
        found.require()


def test_user_dir_candidates_are_not_searched_twice(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "dup"
    ctx = DiscoveryContext(
        root=tmp_path,
        env={"USERPROFILE": str(home), "OneDrive": str(home / "OneDrive")},
        home_override=home,
    )

    found = find_user_directory(ctx)

    assert len(found.searched) == len(set(found.searched))


# ---------------------------------------------------------------------------
# build detection
# ---------------------------------------------------------------------------


def test_build_read_from_install_version_file(tmp_path: Path) -> None:
    library = make_library(tmp_path, "lib")
    install = install_game(library, version="42.20.1\n")

    build = detect_build(install_dir=install, user_dir=None)

    assert build.known
    assert build.raw == "42.20.1"
    assert build.version == (42, 20)
    assert build.source == install / "version.txt"
    assert build.display == "42.20.1"


def test_build_read_from_console_log(tmp_path: Path) -> None:
    user_dir = make_user_dir(tmp_path / "home")
    console = write_console_log(user_dir, "42.20.0")

    build = detect_build(install_dir=None, user_dir=user_dir)

    assert build.known
    assert build.raw == "42.20.0"
    assert build.version == (42, 20)
    assert build.source == console


def test_install_metadata_wins_over_the_console_log(tmp_path: Path) -> None:
    library = make_library(tmp_path, "lib")
    install = install_game(library, version="42.21.0")
    user_dir = make_user_dir(tmp_path / "home")
    write_console_log(user_dir, "41.78.16")

    build = detect_build(install_dir=install, user_dir=user_dir)

    assert build.version == (42, 21)


def test_unknown_build_is_never_guessed(tmp_path: Path) -> None:
    library = make_library(tmp_path, "lib")
    install = install_game(library)
    user_dir = make_user_dir(tmp_path / "home")

    build = detect_build(install_dir=install, user_dir=user_dir)

    assert not build.known
    assert build.raw is None
    assert build.version is None
    assert build.source is None
    assert build.display == "unknown"
    assert build.note
    assert not build.matches(SUPPORTED_BUILDS)
    assert build.searched


def test_version_file_without_a_version_stays_unknown(tmp_path: Path) -> None:
    library = make_library(tmp_path, "lib")
    install = install_game(library, version="unreleased build\n")

    build = detect_build(install_dir=install, user_dir=None)

    assert not build.known


def test_console_log_is_only_read_up_to_the_cap(tmp_path: Path) -> None:
    user_dir = make_user_dir(tmp_path / "home")
    write_console_log(user_dir, "42.20.0", preamble_bytes=MAX_METADATA_BYTES + 512)

    build = detect_build(install_dir=None, user_dir=user_dir)

    assert not build.known
    assert any("no versionNumber=" in problem for problem in build.problems)


def test_an_unreadable_version_file_is_reported_not_passed_over(tmp_path: Path) -> None:
    """ "File absent" and "file present but unreadable" need different remedies.

    Reporting the second as the first sends the user to reinstall a game that is
    fine, so the read failure has to survive into the result.
    """
    library = make_library(tmp_path, "lib")
    install = install_game(library)
    (install / "version.txt").write_bytes(b"\xff\xfe not utf-8")
    (install / "version").write_text("y" * (MAX_METADATA_BYTES + 1), encoding="utf-8")

    build = detect_build(install_dir=install, user_dir=None)

    assert not build.known
    assert build.version is None
    assert any("not valid UTF-8" in problem for problem in build.problems)
    assert any("refusing to parse" in problem for problem in build.problems)
    assert build.note


def test_a_version_file_without_a_version_says_so(tmp_path: Path) -> None:
    library = make_library(tmp_path, "lib")
    install = install_game(library, version="unreleased build\n")
    user_dir = make_user_dir(tmp_path / "home")
    console = write_console_log(user_dir, "42.20.0")

    build = detect_build(install_dir=install, user_dir=user_dir)

    # The console log still answers the question; the useless file is recorded.
    assert build.source == console
    assert any("no version number in it" in problem for problem in build.problems)


def test_build_matches_compares_major_and_minor_only() -> None:
    build = BuildInfo(known=True, raw="42.20.3", version=(42, 20))

    assert build.matches(SUPPORTED_BUILDS)
    assert build.matches(["42.20"])
    assert not build.matches(["41.78"])
    assert not build.matches([])


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_discover_puts_the_whole_picture_together(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path / "Program Files (x86)")
    second = make_library(tmp_path / "D", "SteamLibrary")
    write_modern_libraryfolders(steam_root, [steam_root, second])
    install = install_game(second, version="42.20.0")
    home = tmp_path / "Users" / CYRILLIC_USER
    user_dir = make_user_dir(home)

    result = discover(DiscoveryContext(root=tmp_path, env={"USERPROFILE": str(home)}))

    assert result.complete
    assert result.install.require() == install
    assert result.user_dir.require() == user_dir
    assert result.build.version == (42, 20)
    assert len(result.libraries.libraries) == 2


def test_discover_on_a_bare_machine_is_incomplete_but_informative(tmp_path: Path) -> None:
    result = discover(DiscoveryContext(root=tmp_path))

    assert not result.complete
    assert not result.install.found
    assert not result.user_dir.found
    assert not result.build.known
    assert result.libraries.searched
