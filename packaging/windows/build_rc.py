#!/usr/bin/env python3
"""Assemble the Windows release-candidate ZIP, and say what is not in it.

The archive is the thing a person downloads: the two executables, the eleven
BAT files, the mod, the configurations, the operator documentation and the
schemas the live-test evidence is validated against. It is built here rather
than by hand so that what ships is a function of the tree, and so the same tree
builds the same bytes — every entry is written with a fixed timestamp, which is
what makes the printed SHA-256 mean "this content" rather than "this afternoon".

**Everything except PyInstaller runs on Linux.** That is not a convenience:
almost all of this project's work happens on a machine with no Windows, and a
packaging script that could only be exercised on the target would be exercised
once, at the worst possible moment. So the two ``.exe`` files are treated as
inputs — present when a Windows job built them, absent otherwise — and their
absence is reported as an absence.

**An incomplete archive is still built, and never called complete.** The
alternative is refusing to produce anything, which pushes whoever needs the ZIP
into assembling it by hand, where nothing is recorded. So the archive is
written, ``BUILD-MANIFEST.json`` inside it names every component and every file
it holds, ``complete`` is false when anything is missing, and the exit code is
non-zero so no script mistakes it for a shippable candidate.

The manifest also states that no live evidence is claimed. Building a ZIP proves
that the code compiles and packages; it proves nothing about a scenario ever
having run inside Project Zomboid. ``scripts/check_release.py`` is where those
two questions are kept apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packages" / "pz_agent_core" / "src"))

from pz_agent_core.version import (  # noqa: E402  (path set up above)
    MOD_VERSION,
    PRODUCT_VERSION,
    PROTOCOL_VERSION,
)

#: The release this archive is a candidate *for*. Deliberately not
#: ``PRODUCT_VERSION``: the code still declares its own version, the two are
#: allowed to differ while a candidate is being tested, and the release gate is
#: what refuses to certify v1.0.0 while they do.
RELEASE_VERSION: Final = "1.0.0"

RC_TAG: Final = "rc1"

ARCHIVE_NAME: Final = f"pz-agent-windows-v{RELEASE_VERSION}-{RC_TAG}.zip"

MANIFEST_NAME: Final = "BUILD-MANIFEST.json"

MANIFEST_FORMAT: Final = "pz-agent/windows-rc/1"

#: The eleven wrappers, at the root of the archive so they are what a user sees
#: first. Order is the order they are used in.
BAT_NAMES: Final[tuple[str, ...]] = (
    "install.bat",
    "uninstall.bat",
    "doctor.bat",
    "backup-save.bat",
    "start.bat",
    "status.bat",
    "stop.bat",
    "run-live-tests.bat",
    "resume-live-tests.bat",
    "collect-evidence.bat",
    "finalize-release.bat",
)

BIN_NAMES: Final[tuple[str, ...]] = ("pz-agent.exe", "pz-agent-mcp.exe")

#: What the operator on the Windows machine needs to have on disk, offline.
#:
#: ``GAME_API_VERIFICATION.md`` and ``LOCAL_AGENT_PROMPT.md`` were missing from
#: this list while ``LOCAL_GAME_HANDOFF.md`` and ``LIVE_TEST_PLAYBOOK.md`` —
#: both shipped — told the reader to go and read them. An archive that installs
#: without a checkout left an operator holding two dangling references, one of
#: them the inventory of everything unconfirmed and the other the prompt the
#: local agent starts from. ``tests/contract/test_release_docs_are_self_contained.py``
#: is what keeps that from happening again.
DOC_NAMES: Final[tuple[str, ...]] = (
    "QUICKSTART.md",
    "TROUBLESHOOTING.md",
    "SAFETY.md",
    "LIMITATIONS.md",
    "COMPATIBILITY.md",
    "MCP_TOOLS.md",
    "LOCAL_GAME_HANDOFF.md",
    "LOCAL_DEBUG_MAP.md",
    "LIVE_TEST_PLAYBOOK.md",
    "GAME_API_VERIFICATION.md",
    "LOCAL_AGENT_PROMPT.md",
    # LOCAL_AGENT_PROMPT tells the agent to read PROGRESS; LIMITATIONS sends the
    # reader to RELEASE for the two-catalogue collision. Both were instructions
    # to open a file the archive did not contain.
    "PROGRESS.md",
    "RELEASE.md",
    # The archive's own README links to both, and a document in an archive that
    # points at a file the archive does not contain is defect 13 again. These
    # two earn their place beyond that: PROTOCOL is what LOCAL_DEBUG_MAP and
    # LIVE_TEST_PLAYBOOK assume when they talk about journals, refs and
    # recovery, and ARCHITECTURE is what a reader needs before either.
    "ARCHITECTURE.md",
    "PROTOCOL.md",
    # QUICKSTART sends a reader here for what voice does and does not carry, and
    # voice is a shipped feature with a stop word in it.
    "VOICE.md",
)

#: Legal and safety text belongs at the root, where nobody has to go looking.
META_NAMES: Final[tuple[str, ...]] = ("README.md", "LICENSE", "PRIVACY.md", "SECURITY.md")

#: The evidence tree's own schemas. Without them the live-test runner refuses to
#: write anything — it will not produce an artefact it could not validate — so
#: an archive lacking them cannot be used for the run it exists to support.
EVIDENCE_SCHEMA_NAMES: Final[tuple[str, ...]] = ("result.schema.json", "manifest.schema.json")

#: Mirrors ``pz_agent_cli.modinstall.ALLOWED_SUFFIXES``. A Project Zomboid mod is
#: Lua, metadata and a poster; anything else in the payload directory is not part
#: of the mod and is not shipped.
MOD_SUFFIXES: Final[frozenset[str]] = frozenset({".lua", ".info", ".txt", ".md", ".png", ".json"})

#: Fixed timestamp for every entry, so two builds of the same tree produce
#: identical bytes and therefore an identical SHA-256. 1980-01-01 is the
#: earliest a ZIP can represent.
_ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: Regular file, owner-writable, world-readable — and executable for the two
#: binaries, which matters when the archive is unpacked anywhere but Windows.
_FILE_MODE: Final = 0o644
_EXEC_MODE: Final = 0o755

EXIT_OK: Final = 0
EXIT_INCOMPLETE: Final = 1
EXIT_FAILURE: Final = 2


@dataclass(frozen=True, slots=True)
class Payload:
    """One file that goes into the archive."""

    source: Path
    arcname: str


@dataclass(frozen=True, slots=True)
class Component:
    """A named part of the archive, and what it could not find.

    ``missing`` holds destinations rather than sources: a reader of the manifest
    is asking what is not in the ZIP, not where the build looked.
    """

    name: str
    summary: str
    files: tuple[Payload, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "summary": self.summary,
            "file_count": len(self.files),
            "complete": self.complete,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    """One file as it ended up in the archive."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What was built, what is missing from it, and what it hashes to."""

    archive: Path
    sha256: str
    size_bytes: int
    components: tuple[Component, ...]
    files: tuple[ArchivedFile, ...]

    @property
    def complete(self) -> bool:
        return all(component.complete for component in self.components)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(item for component in self.components for item in component.missing)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _as_crlf(data: bytes) -> bytes:
    """Batch files leave here with Windows line endings.

    The repository normalises every text file to LF (``.gitattributes``), and
    cmd.exe is only reliable with CRLF — a label or a ``goto`` in an LF-only
    batch file is the kind of failure that shows up on one machine and not
    another. Converting at packaging time keeps both rules: LF in the tree, CRLF
    in the artefact.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _sorted_files(root: Path, *, suffixes: frozenset[str] | None = None) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and (suffixes is None or path.suffix.lower() in suffixes)
    )


def _named(
    directory: Path, names: Iterable[str], prefix: str
) -> tuple[tuple[Payload, ...], tuple[str, ...]]:
    """Resolve *names* under *directory*; report the ones that are not there."""
    found: list[Payload] = []
    missing: list[str] = []
    for name in names:
        source = directory / name
        destination = f"{prefix}{name}"
        if source.is_file():
            found.append(Payload(source=source, arcname=destination))
        else:
            missing.append(destination)
    return tuple(found), tuple(missing)


def bat_component(repo_root: Path) -> Component:
    files, missing = _named(repo_root / "packaging" / "windows" / "bat", BAT_NAMES, "")
    return Component(
        name="bat",
        summary="the wrappers a user double-clicks, at the root of the archive",
        files=files,
        missing=missing,
    )


def bin_component(bin_dir: Path) -> Component:
    files, missing = _named(bin_dir, BIN_NAMES, "bin/")
    return Component(
        name="bin",
        summary="the two one-file executables, built by PyInstaller on Windows",
        files=files,
        missing=missing,
    )


def mod_component(repo_root: Path) -> Component:
    source_root = repo_root / "pz-mod" / "42"
    if not (source_root / "mod.info").is_file():
        return Component(
            name="mod",
            summary="the bridge mod, installed by install.bat",
            missing=("mod/mod.info",),
        )
    files = tuple(
        Payload(source=path, arcname=f"mod/{path.relative_to(source_root).as_posix()}")
        for path in _sorted_files(source_root, suffixes=MOD_SUFFIXES)
    )
    return Component(
        name="mod",
        summary="the bridge mod, installed by install.bat",
        files=files,
    )


def configs_component(repo_root: Path) -> Component:
    configs = repo_root / "configs"
    files, missing = _named(configs / "agent", ("config.example.toml",), "configs/agent/")
    mcp_names = ("claude-desktop.json", "claude-code.json", "generic-stdio.json", "README.md")
    mcp_files, mcp_missing = _named(configs / "mcp", mcp_names, "configs/mcp/")
    return Component(
        name="configs",
        summary="the sample agent configuration and the MCP client configurations",
        files=files + mcp_files,
        missing=missing + mcp_missing,
    )


def docs_component(repo_root: Path) -> Component:
    files, missing = _named(repo_root / "docs", DOC_NAMES, "docs/")
    meta_files, meta_missing = _named(repo_root, META_NAMES, "")
    return Component(
        name="docs",
        summary="what the operator needs on disk, without a browser",
        files=files + meta_files,
        missing=missing + meta_missing,
    )


def evidence_schema_component(repo_root: Path) -> Component:
    files, missing = _named(
        repo_root / "evidence" / "schema", EVIDENCE_SCHEMA_NAMES, "evidence/schema/"
    )
    return Component(
        name="evidence-schema",
        summary="the schemas every live-test artefact is validated against before it is written",
        files=files,
        missing=missing,
    )


def plan(repo_root: Path, bin_dir: Path) -> tuple[Component, ...]:
    """Every component of the archive, in the order the manifest lists them."""
    return (
        bat_component(repo_root),
        bin_component(bin_dir),
        mod_component(repo_root),
        configs_component(repo_root),
        docs_component(repo_root),
        evidence_schema_component(repo_root),
    )


def _manifest_document(
    components: Sequence[Component], files: Sequence[ArchivedFile]
) -> dict[str, object]:
    """The manifest that ships inside the archive.

    It carries no build timestamp on purpose. A clock reading would make every
    build produce different bytes, and the reproducible digest is worth more
    than a date that the file's own metadata already approximates.
    """
    return {
        "format": MANIFEST_FORMAT,
        "archive": ARCHIVE_NAME,
        "release_version": RELEASE_VERSION,
        "product_version": PRODUCT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "mod_version": MOD_VERSION,
        "complete": all(component.complete for component in components),
        # Stated rather than implied. This archive is the output of a build; no
        # part of building it observes anything happening inside the game, and a
        # reader of the manifest should not have to infer that.
        "live_evidence_claimed": False,
        "components": [component.to_dict() for component in components],
        "files": [entry.to_dict() for entry in files],
        "file_count": len(files),
    }


def _write_entry(archive: zipfile.ZipFile, arcname: str, data: bytes, *, executable: bool) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (_EXEC_MODE if executable else _FILE_MODE) << 16
    archive.writestr(info, data)


def build(*, repo_root: Path, bin_dir: Path, output_dir: Path) -> BuildReport:
    """Write the archive and return what went into it.

    Raises:
        OSError: if a source file disappears between planning and writing, or
            the output directory cannot be written. Nothing is caught here: a
            packaging script that swallows an I/O error produces an archive
            whose manifest disagrees with its contents.
    """
    components = plan(repo_root, bin_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / ARCHIVE_NAME

    entries: list[tuple[str, bytes, bool]] = []
    for component in components:
        for payload in component.files:
            data = _read(payload.source)
            if payload.arcname.endswith(".bat"):
                data = _as_crlf(data)
            entries.append((payload.arcname, data, payload.arcname.startswith("bin/")))
    entries.sort(key=lambda item: item[0])

    files = tuple(
        ArchivedFile(path=arcname, sha256=_digest(data), size_bytes=len(data))
        for arcname, data, _ in entries
    )
    manifest = json.dumps(
        _manifest_document(components, files), ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_entry(archive, MANIFEST_NAME, manifest + b"\n", executable=False)
        for arcname, data, executable in entries:
            _write_entry(archive, arcname, data, executable=executable)

    raw = destination.read_bytes()
    return BuildReport(
        archive=destination,
        sha256=_digest(raw),
        size_bytes=len(raw),
        components=components,
        files=files,
    )


def write_checksum(report: BuildReport) -> Path:
    """Write the ``sha256sum``-format sidecar beside the archive."""
    path = report.archive.with_name(f"{report.archive.name}.sha256")
    path.write_text(f"{report.sha256}  {report.archive.name}\n", encoding="utf-8")
    return path


def render(report: BuildReport, checksum: Path, *, stream: TextIO) -> None:
    """Print the archive, every component's state, and the digest."""
    say = stream.write
    say(f"{ARCHIVE_NAME}\n")
    say(f"  release       v{RELEASE_VERSION}-{RC_TAG}\n")
    say(f"  code version  {PRODUCT_VERSION} (product), {MOD_VERSION} (mod)\n")
    say(f"  path          {report.archive}\n")
    # The manifest counts what it has digests for; the archive also holds
    # BUILD-MANIFEST.json itself, which cannot digest itself. Both totals are
    # printed because check_release.py reports the zip's entry count, and two
    # tools quoting different numbers for one artefact reads like a defect.
    digested = len(report.files)
    say(f"  files         {digested} digested, {digested + 1} entries in the zip\n")
    say(f"  size          {report.size_bytes} bytes\n")
    say(f"  sha256        {report.sha256}\n")
    say(f"  checksum      {checksum}\n")
    say("  components\n")
    for component in report.components:
        state = "complete" if component.complete else "INCOMPLETE"
        say(f"    {component.name:<16} {len(component.files):>4} file(s)  {state}\n")
        for item in component.missing:
            say(f"      missing: {item}\n")
    say("\n")
    if report.complete:
        say(
            "Every declared component is present. This archive carries no live-test\n"
            "evidence and claims none; scripts/check_release.py --rc is the gate that\n"
            "says whether it may be published as a release candidate.\n"
        )
        return
    say(
        f"INCOMPLETE: {len(report.missing)} file(s) are not in this archive.\n"
        "It was still written, so what is there can be inspected, but it is not a\n"
        "release candidate and BUILD-MANIFEST.json says so. Build the executables on\n"
        "Windows (see packaging/windows/README.md) and run this again.\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_rc.py",
        description=(
            "Assemble the Windows release-candidate ZIP. Runs anywhere; the two "
            "executables are inputs, and their absence is reported rather than hidden."
        ),
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=REPO_ROOT / "packaging" / "windows" / "dist",
        metavar="PATH",
        help="where PyInstaller left pz-agent.exe and pz-agent-mcp.exe",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist",
        metavar="PATH",
        help="where to write the archive",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="exit 0 even when components are missing; the archive still says they are",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    """Build the archive. Returns an exit code; never raises for a missing input."""
    out = sys.stdout if stream is None else stream
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = build(repo_root=REPO_ROOT, bin_dir=args.bin_dir, output_dir=args.output_dir)
        checksum = write_checksum(report)
    except OSError as exc:
        out.write(f"build_rc.py: could not write the archive: {exc.strerror or exc}\n")
        return EXIT_FAILURE
    render(report, checksum, stream=out)
    if report.complete or args.allow_incomplete:
        return EXIT_OK
    return EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
