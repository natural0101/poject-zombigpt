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

**The archive is measured after it is written, never before.** Everything below
:func:`verify_archive` exists because this project has been bitten repeatedly by
a digest taken from the buffer a writer was about to write: such a digest records
an intention, and an intention cannot disagree with itself. So the archive is
closed, reopened, and every member is decompressed and hashed *from the file on
disk*; the index it carries is then checked against those measurements rather
than trusted as their source. ``packages/pz_agent_cli/src/pz_agent_cli/livetest/
evidence.py`` does the same thing for a single artefact and says why at length.

Verification is deliberately two-sided. Every member the index names must be in
the archive with the recorded digest and size, **and** every member in the
archive must be named by the index — hashing only what the index lists would
bless a smuggled-in file, and listing only what the archive holds would bless a
dropped one. A member the index does not list is as much a refusal as one it
lists and the archive lacks.

``scripts/check_release.py`` verifies the finished artefact independently, and
the duplication is on purpose: this one refuses to *hand over* an archive it
cannot measure, and the gate refuses to *publish* one.

An index disagreement is refused rather than reported: the bytes on disk are then
not the bytes the build believes it wrote, and nothing downstream can recover
from that. A *missing required member* is a softer thing — the two executables
cannot exist on Linux — so it is reported, and the exit code says the archive is
not a candidate. Neither says anything about whether the executables inside it
run; nothing here starts one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Final, TextIO

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packages" / "pz_agent_core" / "src"))

from pz_agent_core.platform.paths import (  # noqa: E402  (path set up above)
    PathNotUnderRoot,
    portable_relative_path,
)
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

#: The two wrappers that are not optional in any sense a user would recognise:
#: the one that puts the mod and the configuration in place, and the one that
#: starts the sidecar afterwards. They are members of :data:`BAT_NAMES` too;
#: naming them separately is so that :func:`required_members` reads as the
#: promise it is rather than as an incidental consequence of a tuple's contents.
INSTALLER_MEMBER: Final = "install.bat"
LAUNCHER_MEMBER: Final = "start.bat"

#: The mod's metadata file. Project Zomboid will not list a mod without it, so a
#: mod payload missing this one file is a payload that does not exist.
MOD_INFO_MEMBER: Final = "mod/mod.info"

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

#: The sample the installer copies when the operator has no configuration yet.
CONFIG_AGENT_NAMES: Final[tuple[str, ...]] = ("config.example.toml",)

#: One file per MCP client the project documents, plus the note that explains
#: which of them to pick.
CONFIG_MCP_NAMES: Final[tuple[str, ...]] = (
    "claude-desktop.json",
    "claude-code.json",
    "generic-stdio.json",
    "README.md",
)

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
    """What was built, what is missing from it, and what it hashes to.

    ``sha256`` and every entry in ``files`` are measurements of the finished
    archive, taken by reopening it. Nothing in this report is a prediction.
    """

    archive: Path
    sha256: str
    size_bytes: int
    components: tuple[Component, ...]
    files: tuple[ArchivedFile, ...]
    verification: Verification

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


def archive_member_name(path: PurePath | str, root: PurePath | str, prefix: str) -> str:
    """The name *path* takes inside the archive: ``prefix`` plus a POSIX suffix.

    Every member that comes from walking a directory passes through here, and it
    delegates to :func:`~pz_agent_core.platform.paths.portable_relative_path`
    rather than reimplementing it. The reason is Windows and it is not
    hypothetical: ``rglob`` on Windows yields ``WindowsPath`` objects, and
    ``str(path.relative_to(root))`` on one of those produces
    ``media\\lua\\client\\Runtime.lua``. A ZIP holding that name is a ZIP with one
    file called ``media\\lua\\client\\Runtime.lua`` at its root, which no
    extractor puts in a directory and the mod installer never finds — and on
    Linux, where the tests run, the wrong spelling and the right one are the same
    string, so nothing would ever notice. Passing a :class:`PureWindowsPath` in
    here reproduces the Windows shape on any machine.

    Raises:
        ArchiveError: when *path* is not inside *root*. Never a fallback to the
            absolute path: that would put the account name of whoever ran the
            build into the shipped index. ``PathNotUnderRoot`` is translated
            rather than propagated because its message quotes both paths, and a
            refusal that reaches a bug report must not carry ``C:\\Users\\…``.
    """
    try:
        return f"{prefix}{portable_relative_path(path, root)}"
    except PathNotUnderRoot as exc:
        raise ArchiveError(
            "a file offered to the archive is not inside the directory it was "
            "collected from, so it has no portable name. Build from a clean "
            "checkout, without symlinks pointing out of the tree."
        ) from exc


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
        Payload(source=path, arcname=archive_member_name(path, source_root, "mod/"))
        for path in _sorted_files(source_root, suffixes=MOD_SUFFIXES)
    )
    return Component(
        name="mod",
        summary="the bridge mod, installed by install.bat",
        files=files,
    )


def configs_component(repo_root: Path) -> Component:
    configs = repo_root / "configs"
    files, missing = _named(configs / "agent", CONFIG_AGENT_NAMES, "configs/agent/")
    mcp_files, mcp_missing = _named(configs / "mcp", CONFIG_MCP_NAMES, "configs/mcp/")
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


# ---------------------------------------------------------------------------
# what the archive owes, and how that is measured
# ---------------------------------------------------------------------------


class ArchiveError(Exception):
    """The archive cannot be read, or does not match the index it carries.

    Raised rather than reported, because every condition it covers means the
    bytes on disk are not what the build believes it wrote. There is no useful
    partial answer to that: an archive whose index disagrees with its contents
    cannot be inspected, published, or reasoned about.
    """


#: Members one archive may hold. The real one holds a few hundred; the ceiling
#: is here so a corrupt or hostile central directory cannot make verification
#: run for as long as it likes.
MAX_MEMBERS: Final = 4096

#: Largest single member verification will decompress. Well above a PyInstaller
#: one-file executable and far below anything that would exhaust memory — the
#: read is chunked, so this bounds time rather than memory.
MAX_MEMBER_BYTES: Final = 512 * 1024 * 1024

#: Total uncompressed bytes across all members. A ZIP whose members individually
#: fit but together do not is the classic decompression bomb.
MAX_TOTAL_BYTES: Final = 2 * 1024 * 1024 * 1024

#: Largest archive file verification will hash.
MAX_ARCHIVE_BYTES: Final = 1024 * 1024 * 1024

#: Largest index document. The manifest is a list of names and digests; a
#: megabyte of it is already a defect.
MAX_INDEX_BYTES: Final = 4 * 1024 * 1024

#: Read buffer. Memory use is independent of member size.
_READ_CHUNK_BYTES: Final = 1024 * 1024

#: How many member names one refusal spells out before it starts counting. A
#: refusal nobody finishes reading is a refusal nobody acts on.
_MAX_NAMED: Final = 8

_HEX_DIGITS: Final = frozenset("0123456789abcdef")

_SHA256_LENGTH: Final = 64


def required_members() -> tuple[str, ...]:
    """Every member an archive must hold before it is a release candidate.

    Derived from the same constants the components are built from, so a document
    added to :data:`DOC_NAMES` becomes required without anybody remembering to
    add it here twice. The installer and the launcher are named explicitly even
    though :data:`BAT_NAMES` already contains them: they are the two files whose
    absence turns the archive into a folder of documentation, and that should be
    visible in this function rather than inferred from a tuple elsewhere.
    """
    names = (
        MANIFEST_NAME,
        INSTALLER_MEMBER,
        LAUNCHER_MEMBER,
        MOD_INFO_MEMBER,
        *BAT_NAMES,
        *(f"bin/{name}" for name in BIN_NAMES),
        *(f"configs/agent/{name}" for name in CONFIG_AGENT_NAMES),
        *(f"configs/mcp/{name}" for name in CONFIG_MCP_NAMES),
        *(f"docs/{name}" for name in DOC_NAMES),
        *META_NAMES,
        *(f"evidence/schema/{name}" for name in EVIDENCE_SCHEMA_NAMES),
    )
    return tuple(sorted(set(names)))


def member_name_problem(name: str) -> str:
    """Why *name* cannot be an archive member name, or ``""`` when it can.

    Returns a *category* and never the name itself. That is not fastidiousness:
    the names this function rejects are exactly the ones that may be native
    absolute paths, and ``C:\\Users\\Иван\\…`` echoed into a refusal reaches a
    bug report with the operator's account name in it. The category says what is
    wrong and what to do; the count says how much.
    """
    if not name:
        return "is empty"
    if "\\" in name:
        return "uses a native separator instead of /"
    if ":" in name:
        return "carries a drive letter or an alternate stream marker"
    if name.startswith("/"):
        return "is absolute"
    if name.endswith("/"):
        return "names a directory rather than a file"
    if any(segment in ("", ".", "..") for segment in name.split("/")):
        return "contains an empty, current or parent segment"
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return "contains a control character"
    return ""


def _sha256_file(path: Path, *, limit: int) -> tuple[str, int]:
    """Hash a file in bounded chunks. Returns ``(hexdigest, size_bytes)``.

    The cap is enforced while reading rather than from a prior ``stat``, so a
    file still growing cannot slip past a size that was true a moment ago.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ArchiveError(
                        f"the archive is larger than the {limit} byte ceiling this build "
                        "verifies. Rebuild it, or raise MAX_ARCHIVE_BYTES deliberately."
                    )
                digest.update(chunk)
    except OSError as exc:
        # The path is deliberately not in the message: it is the operator's, and
        # this string reaches bug reports. The errno says what to do about it.
        raise ArchiveError(
            f"the archive cannot be read: {exc.strerror or exc.__class__.__name__}. "
            "Check that the build wrote it and that the volume is readable."
        ) from exc
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class MemberDigest:
    """One member as it was measured *from the archive on disk*.

    Never constructed from a buffer that is about to be written. The whole value
    of this type is that reaching one required decompressing bytes that had
    already been committed to a file.
    """

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of measuring an archive and comparing it with its own index."""

    archive: str
    archive_sha256: str
    archive_size_bytes: int
    members: tuple[MemberDigest, ...]
    #: Named by the index, not found in the archive.
    missing: tuple[str, ...]
    #: Found in the archive with a digest or a size the index does not record.
    changed: tuple[str, ...]
    #: Found in the archive and named by no index entry.
    unlisted: tuple[str, ...]
    #: Required by :func:`required_members`, not found in the archive.
    absent_required: tuple[str, ...]

    @property
    def index_intact(self) -> bool:
        """The archive and its index describe the same set of bytes."""
        return not (self.missing or self.changed or self.unlisted)

    @property
    def complete(self) -> bool:
        return not self.absent_required

    @property
    def ok(self) -> bool:
        return self.index_intact and self.complete

    def index_problem(self) -> str:
        """Why the archive disagrees with its index, or ``""`` when it does not."""
        parts: list[str] = []
        if self.missing:
            parts.append(
                f"{len(self.missing)} member(s) the index names are not in the "
                f"archive ({_some(self.missing)})"
            )
        if self.changed:
            parts.append(
                f"{len(self.changed)} member(s) do not hash to what the index "
                f"records ({_some(self.changed)})"
            )
        if self.unlisted:
            parts.append(
                f"{len(self.unlisted)} member(s) are in the archive and in no "
                f"index entry ({_some(self.unlisted)})"
            )
        if not parts:
            return ""
        return (
            "this archive does not match the index it carries: "
            + "; ".join(parts)
            + ". Do not publish it and do not edit the index to agree — rebuild it "
            "with build_rc.py from a clean tree."
        )

    def completeness_problem(self) -> str:
        """Why the archive is not a release candidate, or ``""`` when it is."""
        if not self.absent_required:
            return ""
        return (
            f"this archive is missing {len(self.absent_required)} required "
            f"member(s) ({_some(self.absent_required)}). Build the two executables "
            "on Windows as the Windows packaging README describes, then run "
            "build_rc.py again; an archive missing any of these installs nothing."
        )

    def refusal(self) -> str:
        """Everything wrong with this archive, or ``""`` when nothing is."""
        return " ".join(
            part for part in (self.index_problem(), self.completeness_problem()) if part
        )


def _some(names: Sequence[str]) -> str:
    """Name up to :data:`_MAX_NAMED` members, then say how many more there are.

    These are archive member names — portable, relative, already checked by
    :func:`member_name_problem` — and not filesystem paths, which is why they may
    be quoted at all.
    """
    shown = ", ".join(names[:_MAX_NAMED])
    remainder = len(names) - _MAX_NAMED
    return shown if remainder <= 0 else f"{shown}, and {remainder} more"


def _checked_names(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    """Every entry, once the ceilings and the name rules are satisfied."""
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ArchiveError(
            f"this archive declares more than the {MAX_MEMBERS} member ceiling "
            "verification accepts. It was not written by build_rc.py; discard it."
        )
    counts: dict[str, int] = {}
    for info in infos:
        problem = member_name_problem(info.filename)
        if problem:
            counts[problem] = counts.get(problem, 0) + 1
    if counts:
        detail = "; ".join(f"{count} {problem}" for problem, count in sorted(counts.items()))
        raise ArchiveError(
            f"this archive holds member name(s) that are not portable: {detail}. "
            "Every archive path must be relative and POSIX; rebuild with build_rc.py, "
            "which routes every name through portable_relative_path."
        )
    seen: set[str] = set()
    for info in infos:
        if info.filename in seen:
            raise ArchiveError(
                f"this archive names {info.filename} more than once. A shadowed "
                "member is invisible to extractors and to its own index, so nothing "
                "here can vouch for it. Rebuild with build_rc.py."
            )
        seen.add(info.filename)
    return tuple(infos)


def _measure(
    archive: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo]
) -> tuple[MemberDigest, ...]:
    """Decompress and hash every member, from the archive as it is on disk.

    Independent of the index by construction: this function never reads
    ``BUILD-MANIFEST.json`` and takes no expected digest. It reports what the
    file contains, and comparison happens afterwards, elsewhere.
    """
    measured: list[MemberDigest] = []
    total = 0
    for info in infos:
        if info.file_size > MAX_MEMBER_BYTES:
            raise ArchiveError(
                f"{info.filename} declares more than the {MAX_MEMBER_BYTES} byte "
                "member ceiling. Discard this archive; build_rc.py packs nothing "
                "of that size."
            )
        digest = hashlib.sha256()
        size = 0
        # ZipFile.open verifies the member's CRC as it goes, so a truncated or
        # corrupted member raises here rather than hashing to something plausible.
        with archive.open(info) as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                # `total` is what this guard is for. The `size` half is defence in
                # depth and is currently unreachable: ZipExtFile caps what it
                # returns at zipinfo.file_size, which the pre-check above already
                # measured against MAX_MEMBER_BYTES, so a member cannot decompress
                # to more than it declared — an under-declared member yields a
                # short read that fails its CRC instead. Kept because that is
                # zipfile's guarantee and not this module's.
                if size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                    raise ArchiveError(
                        f"{info.filename} expands past the size this build verifies. "
                        "Together the members exceed the total this build will "
                        "decompress: discard this archive."
                    )
                digest.update(chunk)
        measured.append(
            MemberDigest(path=info.filename, sha256=digest.hexdigest(), size_bytes=size)
        )
    return tuple(measured)


def _index_entry(entry: Any) -> tuple[str, str, int]:
    """One index entry, or a refusal. Values are never echoed, only described."""
    if not isinstance(entry, Mapping):
        raise ArchiveError(
            "the index in this archive holds an entry that is not an object. "
            "It was not written by build_rc.py; rebuild the archive."
        )
    path = entry.get("path")
    sha256 = entry.get("sha256")
    size = entry.get("size_bytes")
    if not isinstance(path, str) or member_name_problem(path):
        raise ArchiveError(
            "the index in this archive names a member with a path that is missing "
            "or not portable. Rebuild the archive with build_rc.py."
        )
    if not isinstance(sha256, str) or len(sha256) != _SHA256_LENGTH or set(sha256) - _HEX_DIGITS:
        raise ArchiveError(
            f"the index entry for {path} does not carry a SHA-256. An index without "
            "digests verifies nothing; rebuild the archive with build_rc.py."
        )
    # bool is an int in Python, and `True` as a size would compare equal to 1.
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ArchiveError(
            f"the index entry for {path} does not carry a byte size. Rebuild the "
            "archive with build_rc.py."
        )
    return path, sha256, size


def _read_index(archive: zipfile.ZipFile) -> dict[str, tuple[str, int]]:
    """The index the archive carries, as ``path -> (sha256, size_bytes)``."""
    try:
        info = archive.getinfo(MANIFEST_NAME)
    except KeyError as exc:
        raise ArchiveError(
            f"this archive carries no {MANIFEST_NAME}, so there is nothing to check "
            "its contents against. Rebuild it with build_rc.py."
        ) from exc
    with archive.open(info) as handle:
        raw = handle.read(MAX_INDEX_BYTES + 1)
    if len(raw) > MAX_INDEX_BYTES:
        raise ArchiveError(
            f"the index in this archive is larger than the {MAX_INDEX_BYTES} byte "
            "ceiling. Rebuild it with build_rc.py."
        )
    _refuse_deep_nesting(raw)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(
            f"the {MANIFEST_NAME} in this archive is not valid UTF-8 JSON, so no "
            "member can be checked against it. Rebuild it with build_rc.py."
        ) from exc
    if not isinstance(document, dict) or document.get("format") != MANIFEST_FORMAT:
        raise ArchiveError(
            f"the {MANIFEST_NAME} in this archive is not in the {MANIFEST_FORMAT} "
            "format this build understands. Rebuild it with build_rc.py."
        )
    listed = document.get("files")
    if not isinstance(listed, list):
        raise ArchiveError(
            f"the {MANIFEST_NAME} in this archive lists no files, so it accounts for "
            "nothing. Rebuild it with build_rc.py."
        )
    if len(listed) > MAX_MEMBERS:
        raise ArchiveError(
            f"the index in this archive holds more than the {MAX_MEMBERS} entry "
            "ceiling. Rebuild it with build_rc.py."
        )
    index: dict[str, tuple[str, int]] = {}
    for entry in listed:
        path, sha256, size = _index_entry(entry)
        if path in index:
            raise ArchiveError(
                f"the index in this archive names {path} twice. Two claims about one "
                "member is no claim; rebuild the archive with build_rc.py."
            )
        index[path] = (sha256, size)
    return index


#: How deeply a release index may nest before it is refused. A real one is two
#: levels — an object holding a list of flat entries — so ten is generous by a
#: factor of five and still far below any interpreter's limit.
MAX_INDEX_DEPTH: Final = 10


def _refuse_deep_nesting(raw: bytes) -> None:
    """Refuse an index that nests past :data:`MAX_INDEX_DEPTH`, before parsing it.

    ``MAX_INDEX_BYTES`` bounds how many bytes the index may be and says nothing
    about how deeply they may nest: two kilobytes of ``[`` stays three orders of
    magnitude under that ceiling and is still a stack overflow waiting to happen.
    A ceiling that only counts bytes is not a bound on the work of parsing them.

    This used to be a ``try/except RecursionError`` around ``json.loads``, which
    was wrong in a way only a version matrix could show. Whether 2000 levels
    raises ``RecursionError`` is a property of the interpreter, not of the input:
    CPython 3.11 blew its stack and refused, and 3.12 — whose recursion handling
    changed — parsed the same bytes happily and fell through to a *different*
    refusal with a *different* message. The test asserting the message passed on
    one leg of the matrix and failed on the other, and the guard it was testing
    was only ever incidental.

    Counting brackets before parsing is deterministic on every interpreter, and
    it refuses *before* the work rather than after surviving it. Bytes are
    scanned rather than decoded: the delimiters are ASCII, a string containing a
    literal ``[`` can only inflate the count and never hide one, and refusing an
    index for looking too deep is the safe direction of that error.
    """
    depth = 0
    for byte in raw:
        if byte in b"[{":
            depth += 1
            if depth > MAX_INDEX_DEPTH:
                raise ArchiveError(
                    f"the {MANIFEST_NAME} in this archive nests too deeply to parse "
                    f"(past {MAX_INDEX_DEPTH} levels). A release index is a flat list "
                    "of entries; whatever wrote this was not build_rc.py, so discard "
                    "the archive rather than rebuilding its index."
                )
        elif byte in b"]}":
            depth -= 1


def verify_archive(path: Path, *, required: Sequence[str] = ()) -> Verification:
    """Measure an archive and compare it against the index it carries.

    Nothing here trusts the index as a source of truth. Every member is
    decompressed and hashed from *path* first; only then is the index read, and
    it is compared in both directions — an entry with no member is ``missing``,
    a member with no entry is ``unlisted``, and a member whose measured digest or
    size differs from the entry is ``changed``. Hashing only the members the
    index happens to name would pass an archive with a file smuggled into it.

    Args:
        path: the archive, which must already be closed and on disk.
        required: members that must be present regardless of what the index
            says. Pass :func:`required_members` for a release candidate.

    Raises:
        ArchiveError: when the archive cannot be read, breaches a ceiling, holds
            a member name that is not portable, or carries no usable index.
            Content disagreements are *returned* rather than raised, so a caller
            can report all of them at once.
    """
    archive_sha256, archive_size = _sha256_file(path, limit=MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = _checked_names(archive)
            measured = _measure(archive, infos)
            index = _read_index(archive)
    except zipfile.BadZipFile as exc:
        # Covers both a damaged central directory and a member whose CRC does not
        # match its bytes — ZipFile.open checks the CRC as it decompresses, which
        # is why a flipped byte surfaces here rather than as a plausible digest.
        raise ArchiveError(
            "this file is not a readable ZIP archive, or one of its members failed "
            "its CRC check, so nothing in it can be verified. Rebuild it with "
            "build_rc.py."
        ) from exc
    except OSError as exc:
        raise ArchiveError(
            f"this archive cannot be read: {exc.strerror or exc.__class__.__name__}. "
            "Check the volume it is on and build_rc.py again."
        ) from exc

    present = {member.path: member for member in measured}
    # The index cannot record a digest of itself, so it is compared with nothing
    # and excluded from both directions of the cross-check. It is still measured,
    # still hashed, and still required.
    indexable = {name: member for name, member in present.items() if name != MANIFEST_NAME}
    missing = tuple(sorted(name for name in index if name not in indexable))
    unlisted = tuple(sorted(name for name in indexable if name not in index))
    changed = tuple(
        sorted(
            name
            for name, member in indexable.items()
            if name in index and (member.sha256, member.size_bytes) != index[name]
        )
    )
    absent_required = tuple(sorted(name for name in set(required) if name not in present))
    return Verification(
        archive=path.name,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_size,
        members=measured,
        missing=missing,
        changed=changed,
        unlisted=unlisted,
        absent_required=absent_required,
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
    """Write the archive, measure it, and return what is actually in it.

    The index has to be written *into* the archive, so its digests are computed
    from the buffers on their way in — there is no other order available. What
    keeps that honest is the step afterwards: the archive is closed, reopened,
    and every member is decompressed and hashed from disk, and the report is
    built from those measurements rather than from the buffers. If the two ever
    disagree, the ZIP writer, the filesystem or a concurrent editor changed
    something, and this refuses instead of shipping an index that lies.

    Raises:
        ArchiveError: if the finished archive does not match the index it
            carries. The file is left in place to be inspected, and is not a
            release candidate.
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

    intended = tuple(
        ArchivedFile(path=arcname, sha256=_digest(data), size_bytes=len(data))
        for arcname, data, _ in entries
    )
    manifest = json.dumps(
        _manifest_document(components, intended), ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_entry(archive, MANIFEST_NAME, manifest + b"\n", executable=False)
        for arcname, data, executable in entries:
            _write_entry(archive, arcname, data, executable=executable)

    verification = verify_archive(destination, required=required_members())
    problem = verification.index_problem()
    if problem:
        raise ArchiveError(problem)

    files = tuple(
        ArchivedFile(path=member.path, sha256=member.sha256, size_bytes=member.size_bytes)
        for member in sorted(verification.members, key=lambda member: member.path)
        if member.path != MANIFEST_NAME
    )
    return BuildReport(
        archive=destination,
        sha256=verification.archive_sha256,
        size_bytes=verification.archive_size_bytes,
        components=components,
        files=files,
        verification=verification,
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
    say(f"  verified      {len(report.verification.members)} member(s) re-read from the archive\n")
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


def render_verification(verification: Verification, *, stream: TextIO) -> None:
    """Print what an archive was measured to hold, and every disagreement."""
    say = stream.write
    say(f"{verification.archive}\n")
    say(f"  size          {verification.archive_size_bytes} bytes\n")
    say(f"  sha256        {verification.archive_sha256}\n")
    say(f"  members       {len(verification.members)} re-read and hashed from the archive\n")
    say(f"  index         {'matches' if verification.index_intact else 'DISAGREES'}\n")
    say(f"  required      {'all present' if verification.complete else 'INCOMPLETE'}\n")
    say("\n")
    if verification.ok:
        say(
            "Every member was decompressed from the archive, hashed, and found in the\n"
            "index with that digest and size; every index entry was found in the archive;\n"
            "and every required member is present. This says nothing about whether the\n"
            "executables inside it work: they were not run.\n"
        )
        return
    say(f"REFUSED: {verification.refusal()}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_rc.py",
        description=(
            "Assemble the Windows release-candidate ZIP. Runs anywhere; the two "
            "executables are inputs, and their absence is reported rather than hidden."
        ),
    )
    parser.add_argument(
        "--verify",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "verify an existing archive instead of building one: every member is "
            "re-read from it and checked against the index it carries"
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


def verify_main(path: Path, *, allow_incomplete: bool, stream: TextIO) -> int:
    """Verify an existing archive. Returns an exit code; never raises."""
    if not path.is_file():
        stream.write(
            "build_rc.py: there is no archive at the path given to --verify. "
            "Build one first, or check the path.\n"
        )
        return EXIT_FAILURE
    try:
        verification = verify_archive(path, required=required_members())
    except ArchiveError as exc:
        stream.write(f"build_rc.py: REFUSED: {exc}\n")
        return EXIT_FAILURE
    render_verification(verification, stream=stream)
    if not verification.index_intact:
        return EXIT_FAILURE
    if verification.complete or allow_incomplete:
        return EXIT_OK
    return EXIT_INCOMPLETE


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    """Build the archive. Returns an exit code; never raises for a missing input."""
    out = sys.stdout if stream is None else stream
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.verify is not None:
        return verify_main(args.verify, allow_incomplete=args.allow_incomplete, stream=out)
    try:
        report = build(repo_root=REPO_ROOT, bin_dir=args.bin_dir, output_dir=args.output_dir)
        checksum = write_checksum(report)
    except ArchiveError as exc:
        # The archive is left on disk. It is not a candidate, and the refusal
        # says why; deleting it would take away the only thing to inspect.
        out.write(f"build_rc.py: REFUSED: {exc}\n")
        return EXIT_FAILURE
    except OSError as exc:
        out.write(f"build_rc.py: could not write the archive: {exc.strerror or exc}\n")
        return EXIT_FAILURE
    render(report, checksum, stream=out)
    if report.complete or args.allow_incomplete:
        return EXIT_OK
    return EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
