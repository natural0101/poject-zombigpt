"""The support bundle: a redacted archive plus a verifier that says what is in it.

Blueprint §14.7 lists what a bundle carries — versions, doctor output, the
redacted configuration, recent logs, capabilities, a protocol trace — and three
things it must never carry: secrets, a save, whole game files. The builder here
enforces the shape; :func:`verify_bundle` enforces the claim.

Verification is a separate pass on purpose. Every member is redacted on the way
in, so a second scan of the finished archive should find nothing — and that is
exactly why it is worth running: it is an independent check that no member
reached the zip through a path that skipped redaction, and it prints the member
list so a user can see what they are about to attach to a public issue rather
than trusting a promise about it.

Everything is bounded: entry count, per-entry bytes and total bytes, all
enforced as members are added rather than after the archive is written.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ..protocol import JsonDict
from ..version import PRODUCT_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION
from .redaction import Redactor, null_redactor

#: Members one bundle may hold. A bundle is a handful of documents; more than
#: this means something is enumerating a directory into it.
MAX_ENTRIES: Final = 64

#: Ceiling on one member after redaction.
MAX_ENTRY_BYTES: Final = 2 * 1024 * 1024

#: Ceiling on the sum of all members, uncompressed.
MAX_TOTAL_BYTES: Final = 16 * 1024 * 1024

#: Refused before reading: a member this large in an archive that claims to hold
#: text logs is either a mistake or a decompression bomb.
MAX_VERIFY_ENTRY_BYTES: Final = 8 * 1024 * 1024

MANIFEST_NAME: Final = "manifest.json"

_OMISSION_MARKER: Final = "... earlier content omitted ({bytes} byte(s)) ...\n"

#: Fixed member name pattern: nothing a user or a command supplies ever becomes
#: a member name, the same rule the IPC layout holds for filenames.
_NAME_ALLOWED: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/"
)


class BundleError(ValueError):
    """A bundle could not be built or read."""


def _check_name(name: str) -> str:
    if not name or name.startswith("/") or name.endswith("/"):
        raise BundleError(f"bundle member name must be a relative path, got {name!r}")
    if ".." in name.split("/"):
        raise BundleError(f"bundle member name must not traverse: {name!r}")
    if not set(name) <= _NAME_ALLOWED:
        raise BundleError(f"bundle member name has an unsupported character: {name!r}")
    return name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class BundleEntry:
    """One member as it was written, after redaction."""

    name: str
    size: int
    sha256: str
    redacted: bool
    truncated: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "redacted": self.redacted,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class SupportBundle:
    """A written archive and its member list."""

    path: Path
    created_at: str
    entries: tuple[BundleEntry, ...]
    total_bytes: int

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def to_dict(self) -> JsonDict:
        return {
            "created_at": self.created_at,
            "product_version": PRODUCT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "total_bytes": self.total_bytes,
            "entries": [entry.to_dict() for entry in self.entries],
        }


class BundleBuilder:
    """Collects redacted members, then writes them as one archive.

    Nothing is written to disk until :meth:`build`, so a bundle that breaches a
    bound leaves no partial archive behind to be attached by mistake.
    """

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = MAX_ENTRIES,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        if max_entries < 1 or max_entry_bytes < 1 or max_total_bytes < 1:
            raise BundleError("bundle bounds must all be positive")
        self._redactor = null_redactor() if redactor is None else redactor
        self._clock = clock if clock is not None else _utc_now
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._max_total_bytes = max_total_bytes
        self._members: dict[str, bytes] = {}
        self._entries: list[BundleEntry] = []
        self._total = 0

    @property
    def entries(self) -> tuple[BundleEntry, ...]:
        return tuple(self._entries)

    @property
    def total_bytes(self) -> int:
        return self._total

    def add_json(self, name: str, document: Mapping[str, Any] | Sequence[Any]) -> BundleEntry:
        """Add a JSON member, redacted field by field before it is serialised."""
        redacted = self._redactor.value(document)
        text = json.dumps(redacted, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        return self._store(name, text, redacted=True, truncated=False)

    def add_text(self, name: str, text: str) -> BundleEntry:
        """Add a text member, redacted line by line."""
        redacted = "\n".join(self._redactor.text(line) for line in text.splitlines())
        if text.endswith("\n") or not text:
            redacted += "\n"
        return self._store(name, redacted, redacted=True, truncated=False)

    def add_file(self, name: str, path: Path, *, tail_bytes: int | None = None) -> BundleEntry:
        """Add the tail of a file on disk, redacted.

        The *tail* rather than the head: a log is read to find out what happened
        most recently, and reading from the front would fill the bound with the
        least interesting bytes.

        Raises:
            BundleError: when the file cannot be read. A member that silently
                became an empty string would read as "nothing was logged".
        """
        limit = (
            self._max_entry_bytes if tail_bytes is None else min(tail_bytes, self._max_entry_bytes)
        )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BundleError(
                f"{path.name}: cannot read for the bundle ({exc.strerror or exc})"
            ) from exc
        truncated = len(raw) > limit
        if truncated:
            omitted = len(raw) - limit
            raw = raw[-limit:]
            head = _OMISSION_MARKER.format(bytes=omitted)
        else:
            head = ""
        text = head + raw.decode("utf-8", errors="replace")
        redacted = "\n".join(self._redactor.text(line) for line in text.splitlines())
        if text:
            redacted += "\n"
        return self._store(name, redacted, redacted=True, truncated=truncated)

    def _store(self, name: str, text: str, *, redacted: bool, truncated: bool) -> BundleEntry:
        member = _check_name(name)
        if member in self._members:
            raise BundleError(f"bundle already holds a member named {member!r}")
        if len(self._entries) >= self._max_entries:
            raise BundleError(f"a bundle may hold at most {self._max_entries} members")
        data = text.encode("utf-8")
        if len(data) > self._max_entry_bytes:
            raise BundleError(
                f"{member}: {len(data)} bytes exceeds the per-member cap {self._max_entry_bytes}"
            )
        if self._total + len(data) > self._max_total_bytes:
            raise BundleError(
                f"{member}: would take the bundle past its {self._max_total_bytes} byte cap"
            )
        entry = BundleEntry(
            name=member,
            size=len(data),
            sha256=_sha256(data),
            redacted=redacted,
            truncated=truncated,
        )
        self._members[member] = data
        self._entries.append(entry)
        self._total += len(data)
        return entry

    def build(self, destination: Path) -> SupportBundle:
        """Write the archive, manifest last, and return what it holds.

        Raises:
            BundleError: when the archive cannot be written. A failed write
                removes the partial file rather than leaving something that
                looks like a bundle.
        """
        created_at = self._clock().astimezone(UTC)
        manifest = SupportBundle(
            path=destination,
            created_at=created_at.isoformat(),
            entries=tuple(self._entries),
            total_bytes=self._total,
        )
        body = json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        stamp = (
            created_at.year,
            created_at.month,
            created_at.day,
            created_at.hour,
            created_at.minute,
            created_at.second,
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in self._members.items():
                    info = zipfile.ZipInfo(filename=name, date_time=stamp)
                    archive.writestr(info, data)
                archive.writestr(
                    zipfile.ZipInfo(filename=MANIFEST_NAME, date_time=stamp),
                    body.encode("utf-8"),
                )
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise BundleError(
                f"{destination}: cannot write bundle ({exc.strerror or exc})"
            ) from exc
        return manifest


@dataclass(frozen=True, slots=True)
class VerifiedEntry:
    """One member as the verifier found it in the finished archive."""

    name: str
    size: int
    sha256: str
    findings: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class BundleVerification:
    """What an archive contains, and anything in it that should not be."""

    path: Path
    entries: tuple[VerifiedEntry, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True only when every member scanned clean and nothing was unreadable."""
        return not self.problems and all(entry.clean for entry in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    def render_lines(self) -> tuple[str, ...]:
        """The report a user reads before attaching the archive to an issue."""
        lines = [f"{self.path.name}: {len(self.entries)} member(s), {self.total_bytes} byte(s)"]
        for entry in self.entries:
            note = "clean" if entry.clean else "FLAGGED: " + ", ".join(entry.findings)
            lines.append(f"  {entry.name:<40} {entry.size:>9} B  {note}")
        lines.extend(f"  ! {problem}" for problem in self.problems)
        verdict = (
            "nothing matching a home directory, an absolute path or a secret pattern remains"
            if self.clean
            else "REVIEW BEFORE SHARING: the members marked FLAGGED still match a rule"
        )
        lines.append(verdict)
        return tuple(lines)

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path.name,
            "clean": self.clean,
            "total_bytes": self.total_bytes,
            "entries": [
                {
                    "name": entry.name,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "findings": list(entry.findings),
                }
                for entry in self.entries
            ],
            "problems": list(self.problems),
        }


def verify_bundle(
    path: Path,
    *,
    redactor: Redactor | None = None,
    forbidden: Sequence[str] = (),
) -> BundleVerification:
    """List an archive's members and re-scan each for anything that should be gone.

    ``forbidden`` holds literals the caller knows must not appear — the home
    directory, the account name. They are checked in addition to the redactor's
    rules, because a literal the redactor was never told about is exactly the
    case a rule-based scan cannot catch.

    Raises:
        BundleError: when the file is not a readable zip archive.
    """
    scanner = null_redactor() if redactor is None else redactor
    literals = tuple(item for item in forbidden if item)
    entries: list[VerifiedEntry] = []
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                entry, problem = _verify_member(archive, info, scanner, literals)
                if entry is not None:
                    entries.append(entry)
                if problem:
                    problems.append(problem)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError(f"{path}: not a readable support bundle ({exc})") from exc
    return BundleVerification(path=path, entries=tuple(entries), problems=tuple(problems))


def _verify_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    scanner: Redactor,
    literals: Sequence[str],
) -> tuple[VerifiedEntry | None, str]:
    """Scan one member. Returns ``(entry, problem)``; either may be empty."""
    if info.is_dir():
        return None, f"{info.filename}: a bundle holds files, not directories"
    if info.file_size > MAX_VERIFY_ENTRY_BYTES:
        return (
            VerifiedEntry(
                name=info.filename, size=info.file_size, sha256="", findings=("oversize",)
            ),
            f"{info.filename}: {info.file_size} bytes; refused before reading",
        )
    try:
        data = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return None, f"{info.filename}: cannot be read ({exc})"
    findings: list[str] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # A support bundle carries text. A binary member is not necessarily a
        # leak, but it cannot be scanned, so it is reported rather than passed.
        findings.append("not utf-8 text; contents were not scanned")
        text = ""
    findings.extend(scanner.findings(text))
    findings.extend(f"literal:{literal[:24]}" for literal in literals if literal in text)
    return (
        VerifiedEntry(
            name=info.filename,
            size=info.file_size,
            sha256=_sha256(data),
            findings=tuple(dict.fromkeys(findings)),
        ),
        "",
    )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
