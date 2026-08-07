"""What the release archive is required to contain, and how that is measured.

The archive is the only artefact a user ever touches, and everything upstream of
it is verified by something. Until now the archive itself was checked by reading
its own index and agreeing with it, which is not a check: an index is a claim,
and a claim that is compared only with itself always holds.

So these tests are written against three failure modes that a round trip cannot
see.

**A digest taken before the write.** ``install.bat`` is stored in the repository
with LF endings and packed with CRLF, so the bytes in the archive are not the
bytes on disk. The digest of each is pinned here as a hand-written literal. A
build that hashes the buffer it read, rather than the member it wrote, produces
the *other* literal, and that is a difference a round trip cannot produce because
both halves would move together.

**Verification driven by the index.** Every test that tampers with an archive
tampers with it *without* touching the index — a member removed, a member
rewritten, a member added. The added-member case is the one that matters most:
a verifier that iterates the index and hashes what it names passes that archive,
and it is exactly how a file gets smuggled into a release.

**A Windows-shaped path on a Linux test runner.** ``mod/media/lua/client/…`` and
``mod\\media\\lua\\client\\…`` are the same string on Linux when the flavour has
already been thrown away, so the member-naming test builds a
:class:`PureWindowsPath` explicitly and asserts the POSIX result. Without that,
the defect ships and no Linux test ever fails.

Not established by anything in this file: that ``pz-agent.exe`` or
``pz-agent-mcp.exe`` was ever built, runs, or does what it claims. Both are
written here as a handful of bytes with the right names. Nothing here starts a
process, and no PyInstaller build is required or exercised.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import struct
import sys
import warnings
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packaging" / "windows"))

build_rc = importlib.import_module("build_rc")

#: ``install.bat`` as it exists in the repository, and as it must exist in the
#: archive. Written out rather than computed so that a build hashing the wrong
#: one of the two is caught by a literal instead of by agreeing with itself.
INSTALL_BAT_SOURCE: Final = b"@echo off\nrem install.bat\n"
INSTALL_BAT_PACKED: Final = b"@echo off\r\nrem install.bat\r\n"
INSTALL_BAT_SOURCE_SHA: Final = "eed5a3f1a786d23b117230b4de82a41e97569b42efbbd2234927e2a28b345a28"
INSTALL_BAT_PACKED_SHA: Final = "8e68e44e08d4727d2a0e6ae708e7a33075442cffe3f98e85e42268820f59cc72"

MOD_INFO_BODY: Final = b"id=pz_agent_bridge\nname=PZ Agent Bridge\n"
MOD_INFO_SHA: Final = "cd840e926f720b083fb74d390f5c0ffeb7f3bcf3e6165ab670632bf71ead7eb7"

#: A path from a machine whose account name has no business in a bug report.
WINDOWS_ROOT: Final = PureWindowsPath(r"C:\Users\Иван\pz\pz-mod\42")
WINDOWS_MEMBER: Final = WINDOWS_ROOT / "media" / "lua" / "client" / "Runtime.lua"

_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# a synthetic archive, built in tmp_path
# ---------------------------------------------------------------------------


def _make_repo(root: Path) -> Path:
    """A tree holding one of everything the archive declares.

    The two ``.exe`` files are a few bytes with the right names. That is enough
    to exercise every path in the packaging code and is not evidence of a build.
    """
    bat_dir = root / "packaging" / "windows" / "bat"
    bat_dir.mkdir(parents=True)
    for name in build_rc.BAT_NAMES:
        # LF on purpose: converting to CRLF is what the archive owes Windows,
        # and the digest literals above depend on this exact body.
        (bat_dir / name).write_bytes(f"@echo off\nrem {name}\n".encode())

    mod = root / "pz-mod" / "42"
    (mod / "media" / "lua" / "client").mkdir(parents=True)
    (mod / "mod.info").write_bytes(MOD_INFO_BODY)
    (mod / "media" / "lua" / "client" / "Runtime.lua").write_bytes(b"return {}\n")
    (mod / "poster.png").write_bytes(b"\x89PNG\r\n")
    (mod / "notes.tmp").write_bytes(b"not part of the mod\n")

    (root / "configs" / "agent").mkdir(parents=True)
    for name in build_rc.CONFIG_AGENT_NAMES:
        (root / "configs" / "agent" / name).write_bytes(b"# sample\n")
    (root / "configs" / "mcp").mkdir(parents=True)
    for name in build_rc.CONFIG_MCP_NAMES:
        (root / "configs" / "mcp" / name).write_bytes(b"{}\n")

    (root / "docs").mkdir()
    for name in build_rc.DOC_NAMES:
        (root / "docs" / name).write_bytes(f"# {name}\n".encode())
    for name in build_rc.META_NAMES:
        (root / name).write_bytes(f"{name}\n".encode())

    schema = root / "evidence" / "schema"
    schema.mkdir(parents=True)
    for name in build_rc.EVIDENCE_SCHEMA_NAMES:
        (schema / name).write_bytes(b'{"type": "object"}\n')

    binaries = root / "bin"
    binaries.mkdir()
    for name in build_rc.BIN_NAMES:
        (binaries / name).write_bytes(b"MZ not a real executable")
    return root


def _build(tmp_path: Path, *, with_binaries: bool = True) -> Any:
    repo = _make_repo(tmp_path / "repo")
    return build_rc.build(
        repo_root=repo,
        bin_dir=repo / "bin" if with_binaries else repo / "nowhere",
        output_dir=tmp_path / "out",
    )


def _index(archive_path: Path) -> dict[str, dict[str, Any]]:
    """The index the archive carries, keyed by member name."""
    with zipfile.ZipFile(archive_path) as archive:
        document = json.loads(archive.read(build_rc.MANIFEST_NAME))
    return {entry["path"]: entry for entry in document["files"]}


def _rewrite(
    source: Path,
    destination: Path,
    *,
    drop: tuple[str, ...] = (),
    replace: dict[str, bytes] | None = None,
    add: dict[str, bytes] | None = None,
    duplicate: str | None = None,
) -> Path:
    """Copy an archive, editing members but never the index it carries.

    Editing the index too would be the tampering the module's own documentation
    says a digest cannot catch. What is modelled here is the realistic case: the
    ZIP is changed and the index is left as it was.
    """
    replace = replace or {}
    add = add or {}
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for info in original.infolist():
            if info.filename in drop:
                continue
            body = replace.get(info.filename, original.read(info.filename))
            out.writestr(zipfile.ZipInfo(info.filename, date_time=_EPOCH), body)
            if duplicate is not None and info.filename == duplicate:
                # zipfile warns about the duplicate and writes it anyway, which
                # is the point: a real archive can hold one, and the verifier is
                # what has to notice. The warning is this test's own doing.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    out.writestr(zipfile.ZipInfo(info.filename, date_time=_EPOCH), body)
        for name, body in add.items():
            out.writestr(zipfile.ZipInfo(name, date_time=_EPOCH), body)
    return destination


def _lone_archive(path: Path, members: dict[str, bytes]) -> Path:
    """A hand-built ZIP, for shapes ``build`` would never produce.

    Member names are assigned *after* the ``ZipInfo`` exists, because its
    constructor rewrites them: ``if os.sep != "/" and os.sep in filename:
    filename = filename.replace(os.sep, "/")``. On Windows that silently turns a
    name written here with a backslash into one with a forward slash, so a test
    naming a member ``C:\\Users\\...`` got ``C:/Users/...`` on the one platform
    the check exists for — and the refusal it asserted ("uses a native
    separator") was replaced by a different, equally correct one ("carries a
    drive letter"). The test failed on Windows only, for a reason that was
    entirely about the fixture.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            info = zipfile.ZipInfo(date_time=_EPOCH)
            info.filename = name
            archive.writestr(info, body)
    return path


def _index_bytes(entries: list[dict[str, Any]], *, fmt: str | None = None) -> bytes:
    document = {"format": build_rc.MANIFEST_FORMAT if fmt is None else fmt, "files": entries}
    return json.dumps(document, indent=2, sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# the names the archive uses
# ---------------------------------------------------------------------------


def test_the_index_is_named_and_formatted_as_the_release_gate_expects() -> None:
    """Two literals every other tool in the tree hard-codes."""
    assert build_rc.MANIFEST_NAME == "BUILD-MANIFEST.json"
    assert build_rc.MANIFEST_FORMAT == "pz-agent/windows-rc/1"


@pytest.mark.parametrize(
    "name",
    [
        "BUILD-MANIFEST.json",
        "install.bat",
        "bin/pz-agent.exe",
        "mod/mod.info",
        "mod/media/lua/client/Runtime.lua",
        "docs/QUICKSTART.md",
        "evidence/schema/result.schema.json",
    ],
)
def test_the_names_this_archive_uses_are_portable(name: str) -> None:
    assert build_rc.member_name_problem(name) == ""


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", "is empty"),
        ("mod\\mod.info", "uses a native separator instead of /"),
        ("C:/pz/mod.info", "carries a drive letter or an alternate stream marker"),
        ("/etc/passwd", "is absolute"),
        ("mod/", "names a directory rather than a file"),
        ("mod/../../secrets", "contains an empty, current or parent segment"),
        ("mod//mod.info", "contains an empty, current or parent segment"),
        ("mod/./mod.info", "contains an empty, current or parent segment"),
        ("mod/mod\x01info", "contains a control character"),
    ],
)
def test_a_name_that_is_not_a_portable_member_is_named_by_its_category(
    name: str, problem: str
) -> None:
    """The category is pinned, because it is what a refusal is allowed to say."""
    assert build_rc.member_name_problem(name) == problem


def test_the_category_never_contains_the_name_that_produced_it() -> None:
    """A rejected name may be a native absolute path with an account name in it."""
    problem = build_rc.member_name_problem(r"C:\Users\Иван\Zomboid\mod.info")
    assert problem
    for fragment in ("Иван", "C:", "Zomboid", "\\"):
        assert fragment not in problem


# ---------------------------------------------------------------------------
# the Windows shape, constructed explicitly because Linux cannot produce it
# ---------------------------------------------------------------------------


def test_a_windows_member_is_named_with_forward_slashes() -> None:
    """``str(path.relative_to(root))`` on Windows yields backslashes; this must not.

    Asserted against a hand-written POSIX literal rather than against another
    conversion of the same path, so the two cannot agree while both are wrong.
    """
    assert (
        build_rc.archive_member_name(WINDOWS_MEMBER, WINDOWS_ROOT, "mod/")
        == "mod/media/lua/client/Runtime.lua"
    )


def test_a_windows_member_name_carries_no_backslash_and_no_drive() -> None:
    name = build_rc.archive_member_name(WINDOWS_MEMBER, WINDOWS_ROOT, "mod/")
    assert build_rc.member_name_problem(name) == ""
    assert "\\" not in name
    assert "Иван" not in name


def test_a_file_outside_the_root_is_refused_without_quoting_it() -> None:
    outside = PureWindowsPath(r"C:\Users\Иван\Documents\stolen.lua")
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.archive_member_name(outside, WINDOWS_ROOT, "mod/")
    message = str(caught.value)
    for fragment in ("Иван", "C:", "stolen.lua", "\\"):
        assert fragment not in message
    assert "portable name" in message


def test_a_posix_member_is_named_the_same_way() -> None:
    assert (
        build_rc.archive_member_name("/srv/pz-mod/42/mod.info", "/srv/pz-mod/42", "mod/")
        == "mod/mod.info"
    )


# ---------------------------------------------------------------------------
# what the archive must contain
# ---------------------------------------------------------------------------


def test_required_members_names_the_executables_the_mod_and_the_two_wrappers() -> None:
    """The set an operator cannot do without, pinned by hand."""
    required = set(build_rc.required_members())
    assert {
        "BUILD-MANIFEST.json",
        "bin/pz-agent.exe",
        "bin/pz-agent-mcp.exe",
        "mod/mod.info",
        "install.bat",
        "start.bat",
        "configs/agent/config.example.toml",
        "configs/mcp/claude-desktop.json",
        "docs/QUICKSTART.md",
        "docs/SAFETY.md",
        "docs/TROUBLESHOOTING.md",
        "README.md",
        "LICENSE",
        "evidence/schema/result.schema.json",
        "evidence/schema/manifest.schema.json",
    } <= required


def test_every_document_and_wrapper_the_build_declares_is_required() -> None:
    required = set(build_rc.required_members())
    assert {f"docs/{name}" for name in build_rc.DOC_NAMES} <= required
    assert set(build_rc.META_NAMES) <= required
    assert set(build_rc.BAT_NAMES) <= required
    assert {f"bin/{name}" for name in build_rc.BIN_NAMES} <= required


def test_every_required_member_is_itself_a_portable_name() -> None:
    for name in build_rc.required_members():
        assert build_rc.member_name_problem(name) == "", name


def test_required_members_holds_no_duplicate() -> None:
    names = build_rc.required_members()
    assert len(names) == len(set(names))
    assert list(names) == sorted(names)


# ---------------------------------------------------------------------------
# the index describes the bytes in the archive, not the bytes on the way in
# ---------------------------------------------------------------------------


def test_the_index_records_the_packed_bytes_not_the_source_bytes(tmp_path: Path) -> None:
    """The one assertion this whole file exists for.

    ``install.bat`` is LF in the tree and CRLF in the archive. If the digest were
    taken from the buffer that was read, it would be the source literal; taken
    from the member that was written, it is the packed literal.
    """
    report = _build(tmp_path)
    entry = _index(report.archive)["install.bat"]
    assert entry["sha256"] == INSTALL_BAT_PACKED_SHA
    assert entry["size_bytes"] == len(INSTALL_BAT_PACKED)
    assert entry["sha256"] != INSTALL_BAT_SOURCE_SHA
    assert entry["size_bytes"] != len(INSTALL_BAT_SOURCE)


def test_the_report_digests_are_measurements_of_the_finished_archive(tmp_path: Path) -> None:
    report = _build(tmp_path)
    packed = {item.path: item for item in report.files}
    assert packed["install.bat"].sha256 == INSTALL_BAT_PACKED_SHA
    assert packed["mod/mod.info"].sha256 == MOD_INFO_SHA
    assert packed["mod/mod.info"].size_bytes == len(MOD_INFO_BODY)


def test_every_indexed_digest_and_size_is_the_member_the_test_hashes_itself(
    tmp_path: Path,
) -> None:
    """Recomputed here from the archive, so the index is never its own witness."""
    report = _build(tmp_path)
    index = _index(report.archive)
    with zipfile.ZipFile(report.archive) as archive:
        names = [name for name in archive.namelist() if name != build_rc.MANIFEST_NAME]
        assert sorted(names) == sorted(index)
        for name in names:
            body = archive.read(name)
            assert index[name]["sha256"] == hashlib.sha256(body).hexdigest(), name
            assert index[name]["size_bytes"] == len(body), name


def test_the_archive_digest_is_the_digest_of_the_file_on_disk(tmp_path: Path) -> None:
    report = _build(tmp_path)
    assert report.sha256 == hashlib.sha256(report.archive.read_bytes()).hexdigest()
    assert report.size_bytes == report.archive.stat().st_size


def test_verification_measures_the_index_member_too(tmp_path: Path) -> None:
    """It is hashed and required, but compared with nothing: it cannot list itself."""
    report = _build(tmp_path)
    measured = {member.path for member in report.verification.members}
    with zipfile.ZipFile(report.archive) as archive:
        assert measured == set(archive.namelist())
    assert build_rc.MANIFEST_NAME in measured
    assert build_rc.MANIFEST_NAME not in {item.path for item in report.files}


# ---------------------------------------------------------------------------
# what a complete archive holds
# ---------------------------------------------------------------------------


def test_a_complete_archive_holds_both_executables_the_mod_and_every_document(
    tmp_path: Path,
) -> None:
    report = _build(tmp_path)
    assert report.verification.ok
    assert report.verification.absent_required == ()
    index = _index(report.archive)
    for name in build_rc.required_members():
        if name == build_rc.MANIFEST_NAME:
            continue
        assert name in index, name
    assert index["mod/media/lua/client/Runtime.lua"]["size_bytes"] == len(b"return {}\n")


def test_every_member_name_in_a_built_archive_is_posix(tmp_path: Path) -> None:
    report = _build(tmp_path)
    with zipfile.ZipFile(report.archive) as archive:
        for name in archive.namelist():
            assert build_rc.member_name_problem(name) == "", name


def test_a_file_the_mod_directory_holds_but_the_mod_does_not_is_left_out(
    tmp_path: Path,
) -> None:
    report = _build(tmp_path)
    index = _index(report.archive)
    assert "mod/poster.png" in index
    assert "mod/notes.tmp" not in index


# ---------------------------------------------------------------------------
# refusals: a member changed, removed, or never listed
# ---------------------------------------------------------------------------


def test_a_member_rewritten_after_the_build_is_refused(tmp_path: Path) -> None:
    report = _build(tmp_path)
    tampered = _rewrite(
        report.archive,
        tmp_path / "tampered.zip",
        replace={"docs/SAFETY.md": b"# SAFETY.md\nthis paragraph was added later\n"},
    )
    verification = build_rc.verify_archive(tampered, required=build_rc.required_members())
    assert verification.changed == ("docs/SAFETY.md",)
    assert verification.missing == ()
    assert verification.unlisted == ()
    assert not verification.index_intact
    assert not verification.ok
    assert "docs/SAFETY.md" in verification.index_problem()
    assert "rebuild" in verification.index_problem()


def test_a_member_of_the_right_length_but_the_wrong_bytes_is_refused(tmp_path: Path) -> None:
    """Size alone is not identity; the digest is what notices this."""
    report = _build(tmp_path)
    original = _index(report.archive)["mod/mod.info"]["size_bytes"]
    tampered = _rewrite(
        report.archive,
        tmp_path / "same-size.zip",
        replace={"mod/mod.info": b"id=pz_agent_bridgX\nname=PZ Agent Bridge\n"},
    )
    with zipfile.ZipFile(tampered) as archive:
        assert len(archive.read("mod/mod.info")) == original
    verification = build_rc.verify_archive(tampered)
    assert verification.changed == ("mod/mod.info",)


def test_a_member_removed_after_the_build_is_refused(tmp_path: Path) -> None:
    report = _build(tmp_path)
    stripped = _rewrite(report.archive, tmp_path / "stripped.zip", drop=("mod/mod.info",))
    verification = build_rc.verify_archive(stripped, required=build_rc.required_members())
    assert verification.missing == ("mod/mod.info",)
    assert verification.changed == ()
    assert verification.unlisted == ()
    assert "mod/mod.info" in verification.absent_required
    assert not verification.ok
    assert "mod/mod.info" in verification.refusal()


def test_a_member_the_index_does_not_list_is_refused(tmp_path: Path) -> None:
    """The case a verifier driven by the index cannot see.

    Hashing every entry the index names would pass this archive: every one of
    them is present and correct. The extra file is found only by walking the
    archive and asking what is *not* in the index.
    """
    report = _build(tmp_path)
    smuggled = _rewrite(
        report.archive,
        tmp_path / "smuggled.zip",
        add={"extra/payload.txt": b"not declared anywhere\n"},
    )
    verification = build_rc.verify_archive(smuggled, required=build_rc.required_members())
    assert verification.unlisted == ("extra/payload.txt",)
    assert verification.missing == ()
    assert verification.changed == ()
    assert not verification.index_intact
    assert "extra/payload.txt" in verification.index_problem()


def test_a_member_named_twice_is_refused(tmp_path: Path) -> None:
    """A shadowed member is invisible to extraction, so it cannot be vouched for."""
    report = _build(tmp_path)
    shadowed = _rewrite(report.archive, tmp_path / "shadowed.zip", duplicate="docs/SAFETY.md")
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(shadowed)
    assert "docs/SAFETY.md" in str(caught.value)
    assert "more than once" in str(caught.value)


@pytest.mark.parametrize(
    ("member", "category"),
    [
        ("mod\\media\\Иван.lua", "uses a native separator instead of /"),
        ("C:/Users/Иван/mod.info", "carries a drive letter or an alternate stream marker"),
    ],
    ids=["backslash", "drive-letter"],
)
def test_an_unportable_member_name_is_refused_without_echoing_it(
    tmp_path: Path, member: str, category: str
) -> None:
    """Both categories, each with a name that reaches the check unaltered.

    Split in two because the single case that was here named
    ``C:\\Users\\Иван\\mod.info`` — which carries a backslash *and* a drive
    letter — and asserted the backslash category. That is the first branch on
    Linux and, after ``ZipInfo`` rewrote the separator, the second on Windows.
    One name cannot pin an ordered classification on two platforms; two names,
    each unambiguous, can.

    The name is never echoed either way. These are exactly the names that may be
    native absolute paths, and ``C:\\Users\\Иван\\…`` in a refusal reaches a bug
    report with the operator's account name in it.
    """
    archive = _lone_archive(
        tmp_path / "unportable.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([]), member: MOD_INFO_BODY},
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    message = str(caught.value)
    assert "not portable" in message
    assert category in message
    for fragment in ("Иван", "C:", "mod.info", "mod\\media"):
        assert fragment not in message


def test_a_corrupted_member_is_refused(tmp_path: Path) -> None:
    """ZipFile checks the CRC while decompressing, so a flipped byte cannot hash clean."""
    body = b"A" * 512
    archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as handle:
        handle.writestr(zipfile.ZipInfo(build_rc.MANIFEST_NAME, date_time=_EPOCH), _index_bytes([]))
        handle.writestr(zipfile.ZipInfo("docs/QUICKSTART.md", date_time=_EPOCH), body)
    raw = bytearray(archive.read_bytes())
    offset = raw.index(body)
    raw[offset + 7] = ord("B")
    archive.write_bytes(bytes(raw))
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "CRC" in str(caught.value)


# ---------------------------------------------------------------------------
# refusals: the index itself
# ---------------------------------------------------------------------------


def test_an_archive_with_no_index_is_refused(tmp_path: Path) -> None:
    archive = _lone_archive(tmp_path / "bare.zip", {"install.bat": INSTALL_BAT_PACKED})
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert build_rc.MANIFEST_NAME in str(caught.value)


def test_an_index_that_is_not_json_is_refused(tmp_path: Path) -> None:
    archive = _lone_archive(tmp_path / "bad.zip", {build_rc.MANIFEST_NAME: b"{not json"})
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "not valid UTF-8 JSON" in str(caught.value)


def test_an_index_in_an_unknown_format_is_refused(tmp_path: Path) -> None:
    archive = _lone_archive(
        tmp_path / "future.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([], fmt="pz-agent/windows-rc/2")},
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert build_rc.MANIFEST_FORMAT in str(caught.value)


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "install.bat", "size_bytes": 28},
        {"path": "install.bat", "sha256": "not-a-digest", "size_bytes": 28},
        {"path": "install.bat", "sha256": "AB" * 32, "size_bytes": 28},
        {"path": "install.bat", "sha256": "ab" * 31, "size_bytes": 28},
    ],
)
def test_an_index_entry_without_a_usable_digest_is_refused(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    archive = _lone_archive(
        tmp_path / "nodigest.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([entry]), "install.bat": INSTALL_BAT_PACKED},
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "SHA-256" in str(caught.value)


@pytest.mark.parametrize("size", [None, True, -1, "28"])
def test_an_index_entry_without_a_byte_size_is_refused(tmp_path: Path, size: Any) -> None:
    entry: dict[str, Any] = {"path": "install.bat", "sha256": INSTALL_BAT_PACKED_SHA}
    if size is not None:
        entry["size_bytes"] = size
    archive = _lone_archive(
        tmp_path / "nosize.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([entry]), "install.bat": INSTALL_BAT_PACKED},
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "byte size" in str(caught.value)


def test_an_index_naming_a_member_with_a_native_path_is_refused_without_echoing_it(
    tmp_path: Path,
) -> None:
    entry = {
        "path": "C:\\Users\\Иван\\mod.info",
        "sha256": MOD_INFO_SHA,
        "size_bytes": len(MOD_INFO_BODY),
    }
    archive = _lone_archive(
        tmp_path / "nativeindex.zip", {build_rc.MANIFEST_NAME: _index_bytes([entry])}
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    message = str(caught.value)
    assert "not portable" in message
    for fragment in ("Иван", "C:", "mod.info"):
        assert fragment not in message


def test_an_index_recording_the_right_digest_and_the_wrong_size_is_refused(
    tmp_path: Path,
) -> None:
    """Both halves of the entry are compared, not just the digest."""
    entry = {"path": "install.bat", "sha256": INSTALL_BAT_PACKED_SHA, "size_bytes": 1}
    archive = _lone_archive(
        tmp_path / "wrongsize.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([entry]), "install.bat": INSTALL_BAT_PACKED},
    )
    verification = build_rc.verify_archive(archive)
    assert verification.changed == ("install.bat",)
    assert verification.missing == ()
    assert verification.unlisted == ()


def test_an_index_naming_the_same_member_twice_is_refused(tmp_path: Path) -> None:
    entry = {
        "path": "install.bat",
        "sha256": INSTALL_BAT_PACKED_SHA,
        "size_bytes": len(INSTALL_BAT_PACKED),
    }
    archive = _lone_archive(
        tmp_path / "twice.zip",
        {
            build_rc.MANIFEST_NAME: _index_bytes([entry, dict(entry)]),
            "install.bat": INSTALL_BAT_PACKED,
        },
    )
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "twice" in str(caught.value)


def _nested_index(depth: int) -> bytes:
    """A well-formed index whose ``files`` value is *depth* nested empty arrays."""
    head = f'{{"format": "{build_rc.MANIFEST_FORMAT}", "files": '.encode()
    return head + b"[" * depth + b"]" * depth + b"}"


def test_an_index_that_nests_too_deeply_is_refused(tmp_path: Path) -> None:
    """A two-kilobyte index, three orders of magnitude under the byte ceiling.

    ``MAX_INDEX_BYTES`` bounds how large the index may be and nothing bounded how
    deeply it may nest, so ``json.loads`` exhausted the interpreter's stack and
    ``RecursionError`` — not an :class:`ArchiveError` — came out of
    ``verify_archive``. ``--verify`` then died with a traceback and exited 1, the
    code that elsewhere means "incomplete but internally honest", on an archive
    handed to it precisely because somebody already suspected it.
    """
    nested = _nested_index(2000)
    assert len(nested) < build_rc.MAX_INDEX_BYTES
    archive = _lone_archive(tmp_path / "deep.zip", {build_rc.MANIFEST_NAME: nested})
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "nests too deeply" in str(caught.value)


def test_verify_on_an_index_that_nests_too_deeply_exits_failure_without_a_traceback(
    tmp_path: Path,
) -> None:
    """``verify_main`` documents that it never raises. It has to hold here too."""
    nested = _nested_index(2000)
    archive = _lone_archive(tmp_path / "deep-cli.zip", {build_rc.MANIFEST_NAME: nested})
    stream = io.StringIO()
    code = build_rc.main(["--verify", str(archive)], stream=stream)
    assert code == build_rc.EXIT_FAILURE
    assert "REFUSED" in stream.getvalue()


def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-zip.zip"
    plain.write_bytes(b"this is not an archive")
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(plain)
    assert "ZIP" in str(caught.value)


def test_an_archive_that_is_not_there_is_refused_without_naming_the_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(tmp_path / "absent" / "nothing.zip")
    message = str(caught.value)
    assert "cannot be read" in message
    assert str(tmp_path) not in message
    assert "nothing.zip" not in message


# ---------------------------------------------------------------------------
# the ceilings, none of which was exercised by anything
# ---------------------------------------------------------------------------
#
# Every constant below is the module's answer to a hostile archive rather than a
# malformed one: an archive is an untrusted input to ``--verify``, which exists
# to be pointed at a file somebody else produced. A ceiling nothing reaches is a
# ceiling nobody has checked is reachable, and three of these are enforced from a
# *declared* size in the central directory — a number the archive gets to choose.


def _forge_declared_size(path: Path, member: str, size: int) -> Path:
    """Rewrite one member's uncompressed size in the central directory.

    The bytes stay as they were. This is what a hostile archive does for free:
    ``file_size`` is a field, not a measurement, and code that believes it is
    reasoning about a number the attacker supplied.
    """
    raw = bytearray(path.read_bytes())
    needle = b"PK\x01\x02"
    offset = -1
    while True:
        offset = raw.find(needle, offset + 1)
        assert offset != -1, member
        name_length = struct.unpack_from("<H", raw, offset + 28)[0]
        if bytes(raw[offset + 46 : offset + 46 + name_length]) == member.encode():
            struct.pack_into("<I", raw, offset + 24, size)
            path.write_bytes(bytes(raw))
            return path


def test_a_measured_size_is_the_bytes_read_and_not_the_size_declared(tmp_path: Path) -> None:
    """``file_size`` is the archive's claim about itself; ``size_bytes`` is not.

    The central directory is edited to declare a member a thousand times its real
    length. Verification must still report what it actually decompressed, because
    that size is half of what ``changed`` compares against the index.
    """
    body = b"X" * 500
    archive = _lone_archive(
        tmp_path / "forged.zip",
        {
            build_rc.MANIFEST_NAME: _index_bytes(
                [
                    {
                        "path": "docs/QUICKSTART.md",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size_bytes": len(body),
                    }
                ]
            ),
            "docs/QUICKSTART.md": body,
        },
    )
    _forge_declared_size(archive, "docs/QUICKSTART.md", 999_999)
    with zipfile.ZipFile(archive) as handle:
        assert handle.getinfo("docs/QUICKSTART.md").file_size == 999_999
    verification = build_rc.verify_archive(archive)
    measured = {member.path: member for member in verification.members}
    assert measured["docs/QUICKSTART.md"].size_bytes == len(body)
    assert measured["docs/QUICKSTART.md"].sha256 == hashlib.sha256(body).hexdigest()
    # And because the measurement is real, the honest index still agrees with it.
    assert verification.changed == ()
    assert verification.index_intact


def test_a_member_declaring_more_than_the_member_ceiling_is_refused(tmp_path: Path) -> None:
    """Refused from the declaration, before a byte of it is decompressed."""
    archive = _lone_archive(
        tmp_path / "huge.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([]), "docs/QUICKSTART.md": b"small\n"},
    )
    _forge_declared_size(archive, "docs/QUICKSTART.md", build_rc.MAX_MEMBER_BYTES + 1)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert str(build_rc.MAX_MEMBER_BYTES) in str(caught.value)
    assert "member ceiling" in str(caught.value)


def test_a_member_declaring_less_than_it_holds_cannot_outrun_the_pre_check(
    tmp_path: Path,
) -> None:
    """The classic bomb — declare one byte, hold five hundred — cannot happen here.

    ``ZipExtFile`` caps what it returns at ``zipinfo.file_size``, so a member can
    never decompress to more than it declared, and the declaration was already
    measured against the ceiling before a byte was read. What a shrunken
    declaration buys an attacker is a short read, which fails its CRC. This is
    pinned because it is the reason the *running total* rather than the per-member
    count is the guard that does the work in ``_measure``.
    """
    body = b"X" * 500
    archive = _lone_archive(
        tmp_path / "bomb.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([]), "docs/QUICKSTART.md": body},
    )
    _forge_declared_size(archive, "docs/QUICKSTART.md", 1)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "CRC" in str(caught.value)


def test_members_that_together_pass_the_total_ceiling_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each member fits the member ceiling; together they do not fit the total.

    Only ``MAX_TOTAL_BYTES`` is lowered, so nothing but the running sum can be
    what refuses this: every member here is four hundred bytes against a member
    ceiling left at its real 512 MiB.
    """
    archive = _lone_archive(
        tmp_path / "total.zip",
        {
            build_rc.MANIFEST_NAME: _index_bytes([]),
            **{f"docs/{index:03d}.md": b"Y" * 400 for index in range(10)},
        },
    )
    monkeypatch.setattr(build_rc, "MAX_TOTAL_BYTES", 1500)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "expands past the size this build verifies" in str(caught.value)


def test_an_archive_with_more_members_than_the_ceiling_is_refused(tmp_path: Path) -> None:
    """The real constant, with a real archive that exceeds it by one."""
    members = {f"docs/{index:05d}.md": b"x" for index in range(build_rc.MAX_MEMBERS + 1)}
    archive = _lone_archive(tmp_path / "many.zip", members)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert str(build_rc.MAX_MEMBERS) in str(caught.value)
    assert "member ceiling" in str(caught.value)


def test_an_index_larger_than_the_index_ceiling_is_refused(tmp_path: Path) -> None:
    """Refused on length, before it is decoded or parsed."""
    padding = "p" * (build_rc.MAX_INDEX_BYTES + 1)
    document = json.dumps({"format": build_rc.MANIFEST_FORMAT, "files": [], "pad": padding})
    archive = _lone_archive(tmp_path / "fatindex.zip", {build_rc.MANIFEST_NAME: document.encode()})
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert str(build_rc.MAX_INDEX_BYTES) in str(caught.value)
    assert "ceiling" in str(caught.value)


def test_an_index_listing_more_entries_than_the_ceiling_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        {"path": f"docs/{index:03d}.md", "sha256": MOD_INFO_SHA, "size_bytes": 1}
        for index in range(20)
    ]
    archive = _lone_archive(
        tmp_path / "wideindex.zip", {build_rc.MANIFEST_NAME: _index_bytes(entries)}
    )
    monkeypatch.setattr(build_rc, "MAX_MEMBERS", 10)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "entry" in str(caught.value)


def test_an_archive_larger_than_the_archive_ceiling_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enforced while reading, so a file still growing cannot slip past a stat."""
    archive = _lone_archive(
        tmp_path / "big.zip",
        {build_rc.MANIFEST_NAME: _index_bytes([]), "docs/QUICKSTART.md": b"z" * 4096},
    )
    monkeypatch.setattr(build_rc, "MAX_ARCHIVE_BYTES", 64)
    with pytest.raises(build_rc.ArchiveError) as caught:
        build_rc.verify_archive(archive)
    assert "64 byte ceiling" in str(caught.value)
    # The refusal is about the archive, and still names no path.
    assert str(tmp_path) not in str(caught.value)


# ---------------------------------------------------------------------------
# the build refuses its own output when the two disagree
# ---------------------------------------------------------------------------


def test_build_refuses_when_a_member_reaches_the_archive_altered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the whole write-then-measure design.

    The index is computed from the buffers, then one buffer is changed on its way
    into the ZIP. A build that trusted its own buffers would report a digest for
    ``docs/SAFETY.md`` that nothing on disk has, and would exit 0.
    """
    written = build_rc._write_entry

    def sabotage(archive: zipfile.ZipFile, arcname: str, data: bytes, *, executable: bool) -> None:
        if arcname == "docs/SAFETY.md":
            data = data + b"appended between the index and the write\n"
        written(archive, arcname, data, executable=executable)

    monkeypatch.setattr(build_rc, "_write_entry", sabotage)
    with pytest.raises(build_rc.ArchiveError) as caught:
        _build(tmp_path)
    assert "docs/SAFETY.md" in str(caught.value)
    assert "do not edit the index to agree" in str(caught.value)


def test_a_build_missing_the_executables_still_matches_its_index(tmp_path: Path) -> None:
    """Incomplete is not the same as inconsistent, and only one of them refuses."""
    report = _build(tmp_path, with_binaries=False)
    verification = report.verification
    assert verification.index_intact
    assert not verification.complete
    assert verification.absent_required == ("bin/pz-agent-mcp.exe", "bin/pz-agent.exe")
    assert verification.index_problem() == ""
    assert "bin/pz-agent.exe" in verification.completeness_problem()
    assert "Windows" in verification.completeness_problem()


def test_a_refusal_names_a_bounded_number_of_members(tmp_path: Path) -> None:
    """A refusal listing four hundred names is one nobody reads to the end."""
    report = _build(tmp_path, with_binaries=False)
    verification = report.verification
    many = tuple(f"docs/{index:03d}.md" for index in range(30))
    wide = build_rc.Verification(
        archive=verification.archive,
        archive_sha256=verification.archive_sha256,
        archive_size_bytes=verification.archive_size_bytes,
        members=verification.members,
        missing=many,
        changed=(),
        unlisted=(),
        absent_required=(),
    )
    problem = wide.index_problem()
    # Hand-written, not derived from _MAX_NAMED. Deriving them would move the
    # expectation with the constant, and a ceiling that moves with its own test
    # is not a ceiling — that is exactly what the mutation run caught here.
    assert "docs/000.md" in problem
    assert "docs/007.md" in problem
    assert "docs/008.md" not in problem
    assert "docs/029.md" not in problem
    assert "and 22 more" in problem
    assert problem.count("docs/") == 8


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_verify_on_a_complete_archive_reports_and_exits_zero(tmp_path: Path) -> None:
    report = _build(tmp_path)
    stream = io.StringIO()
    code = build_rc.main(["--verify", str(report.archive)], stream=stream)
    printed = stream.getvalue()
    assert code == build_rc.EXIT_OK
    assert "index         matches" in printed
    assert "required      all present" in printed
    assert report.sha256 in printed
    # The one claim the archive must never make on its own behalf.
    assert "they were not run" in printed


def test_verify_on_an_incomplete_archive_exits_incomplete(tmp_path: Path) -> None:
    report = _build(tmp_path, with_binaries=False)
    stream = io.StringIO()
    code = build_rc.main(["--verify", str(report.archive)], stream=stream)
    assert code == build_rc.EXIT_INCOMPLETE
    assert "INCOMPLETE" in stream.getvalue()
    assert "bin/pz-agent.exe" in stream.getvalue()


def test_verify_on_a_tampered_archive_exits_failure(tmp_path: Path) -> None:
    report = _build(tmp_path)
    tampered = _rewrite(
        report.archive, tmp_path / "bad.zip", add={"extra/payload.txt": b"smuggled\n"}
    )
    stream = io.StringIO()
    code = build_rc.main(["--verify", str(tampered)], stream=stream)
    assert code == build_rc.EXIT_FAILURE
    assert "REFUSED" in stream.getvalue()
    assert "extra/payload.txt" in stream.getvalue()


def test_verify_on_a_path_that_holds_nothing_exits_failure(tmp_path: Path) -> None:
    stream = io.StringIO()
    code = build_rc.main(["--verify", str(tmp_path / "absent.zip")], stream=stream)
    assert code == build_rc.EXIT_FAILURE
    printed = stream.getvalue()
    assert "no archive at the path" in printed
    assert str(tmp_path) not in printed


def test_verify_never_builds_anything(tmp_path: Path) -> None:
    """``--verify`` reads; a run of it must not write into the repository's dist/."""
    report = _build(tmp_path)
    before = sorted(path.name for path in (tmp_path / "out").iterdir())
    build_rc.main(["--verify", str(report.archive)], stream=io.StringIO())
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == before
