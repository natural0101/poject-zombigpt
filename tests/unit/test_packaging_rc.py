"""The Windows release candidate: its wrappers, its archive, and its sample config.

Three things are worth testing here, and they are the three that can be wrong
without anybody noticing until a user is holding the ZIP.

**Every BAT must call a command that exists.** A wrapper naming a subcommand the
parser does not have fails with argparse's usage text, which reads to a
non-technical user as "the program is broken". So each wrapper's invocation is
extracted from the file and handed to the real parser.

**The archive must not claim to be complete when it is not.** The build runs on
Linux, where the executables cannot exist, so the interesting case is the
incomplete one — and the assertion is that it says so, in the manifest and in
the exit code.

**The sample configuration must be one the program accepts.** An installer that
writes a file the validator then refuses is worse than one that writes none.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.app import build_parser
from pz_agent_cli.config import SCHEMA, load_config

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packaging" / "windows"))

build_rc = importlib.import_module("build_rc")

#: Values a BAT variable stands for when the wrapper is run. ``%*`` is whatever
#: the user typed and is dropped; the other two are the optional overrides the
#: wrappers add when they are running the bundled executable.
_EXPANSIONS: Final[dict[str, list[str]]] = {
    "%*": [],
    "%EVIDENCE%": ["--evidence-dir", "C:\\pz agent\\evidence"],
    "%OUTPUT%": ["--output", "C:\\pz agent\\release\\evidence-manifest.json"],
}

#: The same variables when the wrapper is running an installed ``pz-agent``:
#: the defaults already point at the right place, so it passes nothing.
_EMPTY_EXPANSIONS: Final[dict[str, list[str]]] = {"%*": [], "%EVIDENCE%": [], "%OUTPUT%": []}


def _bat_dir() -> Path:
    return REPO_ROOT / "packaging" / "windows" / "bat"


def _tokenise(line: str) -> list[str]:
    """Split a batch command line the way cmd.exe does: quotes group, nothing else."""
    tokens: list[str] = []
    current = ""
    quoted = False
    for char in line:
        if char == '"':
            quoted = not quoted
            continue
        if char == " " and not quoted:
            if current:
                tokens.append(current)
                current = ""
            continue
        current += char
    if current:
        tokens.append(current)
    return tokens


def _invocations(bat: Path, expansions: dict[str, list[str]]) -> list[list[str]]:
    """Every ``pz-agent`` command line in *bat*, with its variables expanded."""
    found: list[list[str]] = []
    for raw in bat.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith('"%PZ_AGENT%"'):
            continue
        argv: list[str] = []
        for token in _tokenise(line)[1:]:
            if token in expansions:
                argv.extend(expansions[token])
            elif "%" in token:
                # A path the wrapper resolved. Its value cannot matter to the
                # parser, only its presence.
                argv.append("C:\\pz agent\\somewhere")
            else:
                argv.append(token)
        found.append(argv)
    return found


# ---------------------------------------------------------------------------
# the wrappers
# ---------------------------------------------------------------------------


def test_every_declared_wrapper_is_present() -> None:
    missing = [name for name in build_rc.BAT_NAMES if not (_bat_dir() / name).is_file()]
    assert missing == []
    assert len(build_rc.BAT_NAMES) == 11


@pytest.mark.parametrize("name", build_rc.BAT_NAMES)
@pytest.mark.parametrize("expansions", [_EXPANSIONS, _EMPTY_EXPANSIONS], ids=["bundled", "on-path"])
def test_wrapper_calls_a_command_the_cli_has(name: str, expansions: dict[str, list[str]]) -> None:
    """The parser accepts every command line the wrapper can produce."""
    parser = build_parser()
    invocations = _invocations(_bat_dir() / name, expansions)
    assert invocations, f"{name} runs no pz-agent command"
    for argv in invocations:
        # parse_args exits on an unknown command or a rejected option; letting
        # that surface as the failure is the whole point of the test.
        parsed = parser.parse_args(argv)
        assert parsed.command is not None


@pytest.mark.parametrize("name", build_rc.BAT_NAMES)
def test_wrapper_survives_a_path_with_spaces(name: str) -> None:
    """Every path the wrapper touches is quoted, and it runs from its own folder."""
    text = (_bat_dir() / name).read_text(encoding="utf-8")
    assert 'pushd "%~dp0"' in text
    assert '"%PZ_AGENT%"' in text
    for marker in ("%~dp0bin\\pz-agent.exe", "%~dp0evidence", "%~dp0mod"):
        for line in text.splitlines():
            if marker not in line or line.lstrip().startswith(("rem", "echo")):
                continue
            assert f'"{marker}' in line or f'{marker}"' in line, line


@pytest.mark.parametrize("name", build_rc.BAT_NAMES)
def test_wrapper_reports_failure_and_waits(name: str) -> None:
    """A failure exits non-zero and pauses, so a double-clicked window can be read."""
    text = (_bat_dir() / name).read_text(encoding="utf-8")
    assert 'set "RC=%ERRORLEVEL%"' in text
    assert "endlocal & exit /b %RC%" in text
    assert "pause" in text
    # No parenthesised block: a multi-line ``if ... (`` is the construct that
    # breaks when a batch file reaches Windows with the repository's LF endings.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("rem", "echo")):
            continue
        assert not stripped.endswith("("), line


def test_live_test_wrappers_name_the_subcommands_that_exist() -> None:
    """The four live-test wrappers map onto the four live-test subcommands."""
    expected = {
        "run-live-tests.bat": "run",
        "resume-live-tests.bat": "resume",
        "collect-evidence.bat": "collect",
        "finalize-release.bat": "finalize",
    }
    parser = build_parser()
    for name, subcommand in expected.items():
        argv = _invocations(_bat_dir() / name, _EXPANSIONS)[0]
        assert argv[0] == "live-test"
        parsed = parser.parse_args(argv)
        assert parsed.command == "live-test"
        assert parsed.live_command == subcommand


# ---------------------------------------------------------------------------
# the archive
# ---------------------------------------------------------------------------


def _make_repo(root: Path) -> Path:
    """A tree holding one of everything the archive declares."""
    bat_dir = root / "packaging" / "windows" / "bat"
    bat_dir.mkdir(parents=True)
    for name in build_rc.BAT_NAMES:
        # LF on purpose: the conversion to CRLF is what the archive owes Windows.
        (bat_dir / name).write_text(f"@echo off\nrem {name}\n", encoding="utf-8")

    mod = root / "pz-mod" / "42"
    (mod / "media" / "lua" / "client").mkdir(parents=True)
    (mod / "mod.info").write_text("id=pz_agent_bridge\n", encoding="utf-8")
    (mod / "media" / "lua" / "client" / "Runtime.lua").write_text("return {}\n", encoding="utf-8")
    (mod / "poster.png").write_bytes(b"\x89PNG\r\n")
    (mod / "notes.tmp").write_text("not part of the mod\n", encoding="utf-8")

    (root / "configs" / "agent").mkdir(parents=True)
    (root / "configs" / "agent" / "config.example.toml").write_text("# sample\n", encoding="utf-8")
    (root / "configs" / "mcp").mkdir(parents=True)
    for name in ("claude-desktop.json", "claude-code.json", "generic-stdio.json"):
        (root / "configs" / "mcp" / name).write_text("{}\n", encoding="utf-8")
    (root / "configs" / "mcp" / "README.md").write_text("# mcp\n", encoding="utf-8")

    (root / "docs").mkdir()
    for name in build_rc.DOC_NAMES:
        (root / "docs" / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in build_rc.META_NAMES:
        (root / name).write_text(f"{name}\n", encoding="utf-8")

    schema = root / "evidence" / "schema"
    schema.mkdir(parents=True)
    for name in build_rc.EVIDENCE_SCHEMA_NAMES:
        (schema / name).write_text('{"type": "object"}\n', encoding="utf-8")

    binaries = root / "bin"
    binaries.mkdir()
    for name in build_rc.BIN_NAMES:
        (binaries / name).write_bytes(b"MZ fake executable")
    return root


def _build(tmp_path: Path, *, with_binaries: bool = True) -> Any:
    repo = _make_repo(tmp_path / "repo")
    return build_rc.build(
        repo_root=repo,
        bin_dir=repo / "bin" if with_binaries else repo / "nowhere",
        output_dir=tmp_path / "out",
    )


def test_complete_archive_holds_every_component(tmp_path: Path) -> None:
    report = _build(tmp_path)
    assert report.complete
    assert report.missing == ()
    with zipfile.ZipFile(report.archive) as archive:
        names = set(archive.namelist())
    for name in build_rc.BAT_NAMES:
        assert name in names
    for name in build_rc.BIN_NAMES:
        assert f"bin/{name}" in names
    assert "mod/mod.info" in names
    assert "mod/media/lua/client/Runtime.lua" in names
    assert "configs/agent/config.example.toml" in names
    assert "configs/mcp/claude-desktop.json" in names
    assert "docs/QUICKSTART.md" in names
    assert "evidence/schema/result.schema.json" in names
    assert build_rc.MANIFEST_NAME in names


def test_archive_carries_only_mod_files_from_the_mod_directory(tmp_path: Path) -> None:
    report = _build(tmp_path)
    with zipfile.ZipFile(report.archive) as archive:
        names = set(archive.namelist())
    assert "mod/poster.png" in names
    assert "mod/notes.tmp" not in names


def test_batch_files_reach_windows_with_crlf(tmp_path: Path) -> None:
    report = _build(tmp_path)
    with zipfile.ZipFile(report.archive) as archive:
        body = archive.read("install.bat")
    assert body.endswith(b"\r\n")
    assert b"\r\n" in body
    assert body.replace(b"\r\n", b"").count(b"\n") == 0


def test_manifest_matches_the_bytes_in_the_archive(tmp_path: Path) -> None:
    report = _build(tmp_path)
    with zipfile.ZipFile(report.archive) as archive:
        manifest = json.loads(archive.read(build_rc.MANIFEST_NAME))
        for entry in manifest["files"]:
            body = archive.read(entry["path"])
            assert len(body) == entry["size_bytes"]
    assert manifest["complete"] is True
    assert manifest["live_evidence_claimed"] is False
    assert manifest["format"] == build_rc.MANIFEST_FORMAT
    assert {entry.path for entry in report.files} == {e["path"] for e in manifest["files"]}


def test_the_same_tree_builds_the_same_bytes(tmp_path: Path) -> None:
    """A digest that changed with the clock would say nothing about the content."""
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")
    assert first.sha256 == second.sha256


def test_a_missing_executable_is_named_and_never_called_complete(tmp_path: Path) -> None:
    report = _build(tmp_path, with_binaries=False)
    assert not report.complete
    assert set(report.missing) == {f"bin/{name}" for name in build_rc.BIN_NAMES}
    # The archive is still written: what is in it can be inspected.
    assert report.archive.is_file()
    with zipfile.ZipFile(report.archive) as archive:
        manifest = json.loads(archive.read(build_rc.MANIFEST_NAME))
    assert manifest["complete"] is False
    assert "bin/pz-agent.exe" in [
        item for component in manifest["components"] for item in component["missing"]
    ]


def test_checksum_sidecar_is_the_digest_of_the_archive(tmp_path: Path) -> None:
    report = _build(tmp_path)
    sidecar = build_rc.write_checksum(report)
    assert sidecar.read_text(encoding="utf-8") == f"{report.sha256}  {report.archive.name}\n"


def test_building_this_repository_reports_the_absent_executables(tmp_path: Path) -> None:
    """The real tree, on this machine: no Windows, so no executables."""
    stream = io.StringIO()
    code = build_rc.main(["--output-dir", str(tmp_path)], stream=stream)
    printed = stream.getvalue()
    assert "bin/pz-agent.exe" in printed
    assert code == build_rc.EXIT_INCOMPLETE
    assert "INCOMPLETE" in printed
    assert build_rc.main(["--output-dir", str(tmp_path), "--allow-incomplete"], stream=stream) == 0


@pytest.mark.parametrize("component", ["bat", "mod", "configs"])
def test_this_repository_holds_the_components_it_can(component: str) -> None:
    """Everything that does not need Windows or a game session is here now."""
    found = {item.name: item for item in build_rc.plan(REPO_ROOT, REPO_ROOT / "nowhere")}
    assert found[component].missing == ()
    assert found[component].files


# ---------------------------------------------------------------------------
# the sample configuration
# ---------------------------------------------------------------------------


def _sample_path() -> Path:
    return REPO_ROOT / "configs" / "agent" / "config.example.toml"


def test_sample_configuration_is_one_the_validator_accepts() -> None:
    validation = load_config(_sample_path())
    assert [problem.render() for problem in validation.errors] == []
    # Warnings are advisories about settings that validate; the sample is the
    # documented default, so it should not be advising anything.
    assert [problem.render() for problem in validation.warnings] == []


def test_sample_configuration_documents_every_key_this_build_knows() -> None:
    text = _sample_path().read_text(encoding="utf-8")
    for table, keys in SCHEMA.items():
        assert f"[{table}]" in text, table
        for key in keys:
            assert key in text, f"{table}.{key}"


def test_sample_configuration_names_variables_and_holds_no_key() -> None:
    document = tomllib.loads(_sample_path().read_text(encoding="utf-8"))
    named = [
        section["api_key_env"]
        for name, section in document["planner"].items()
        if isinstance(section, dict) and "api_key_env" in section
    ]
    assert named
    for variable in named:
        assert variable.isupper()
        assert variable.startswith("PZ_AGENT_")
    text = _sample_path().read_text(encoding="utf-8")
    for shape in ("sk-", "api_key =", "token =", "password"):
        assert shape not in text
