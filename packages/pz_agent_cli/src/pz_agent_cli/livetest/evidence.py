"""The evidence tree: where artefacts live, how they are hashed, what is missing.

This module knows about files and digests and nothing about scenarios passing.
That separation is deliberate — the decision of *what* a passing scenario owes
belongs to the runner, and a module that both decides and measures would be able
to lower the bar quietly.

Two things here carry weight.

**Canonical bytes.** Everything written goes through :func:`canonical_json`, so
the SHA-256 of a document is a property of its content and not of the whitespace
that happened to be in the writer. Re-serialising a document read from disk
reproduces the same hash; that is what makes tamper detection work at all.

**The hash is a tripwire, not a seal.** There is no key — this project ships no
secrets — so a determined operator who edits ``result.json`` *and* rewrites the
ledger entry that records its digest will not be caught by arithmetic. What the
digest catches is the realistic case: a file edited in place to turn a FAIL into
a PASS. ``evidence/README.md`` says so plainly rather than implying more.

Everything is bounded: artefact size, file count per collection, manifest size.
An evidence tree that can grow without limit is one nobody will ever finish
reading.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pz_agent_core.protocol import JsonDict

#: Largest single artefact the tree accepts. A game log past this is a runaway
#: error loop, and copying it would bury the evidence rather than provide it.
MAX_ARTEFACT_BYTES: Final = 64 * 1024 * 1024

#: Files one ``collect`` run may copy into one scenario directory.
MAX_COLLECTED_FILES: Final = 200

#: Read buffer for hashing and copying. Memory use is independent of file size.
_CHUNK_BYTES: Final = 1024 * 1024

#: Marker file that keeps an otherwise-generated directory in git.
GITKEEP_NAME: Final = ".gitkeep"

RESULT_NAME: Final = "result.json"
STATE_NAME: Final = "state.json"
PREPARE_NAME: Final = "prepare.json"
COLLECTED_NAME: Final = "collected.json"

LOGS_DIR_NAME: Final = "logs"
JOURNALS_DIR_NAME: Final = "journals"
SNAPSHOTS_DIR_NAME: Final = "snapshots"
SCREENSHOTS_DIR_NAME: Final = "screenshots"
ATTEMPTS_DIR_NAME: Final = "attempts"

SCHEMA_DIR_NAME: Final = "schema"
RESULT_SCHEMA_NAME: Final = "result.schema.json"
MANIFEST_SCHEMA_NAME: Final = "manifest.schema.json"

#: Subdirectories every scenario directory carries, so an operator dropping a
#: screenshot in by hand does not have to guess the name.
_SCENARIO_SUBDIRS: Final = (
    LOGS_DIR_NAME,
    JOURNALS_DIR_NAME,
    SNAPSHOTS_DIR_NAME,
    SCREENSHOTS_DIR_NAME,
    ATTEMPTS_DIR_NAME,
)


class LiveTestError(Exception):
    """Base class for every refusal from the live-test subsystem."""


class TamperError(LiveTestError):
    """A written artefact does not match the digest recorded for it.

    Raised on read, never swallowed. The whole point of recording the digest is
    that a mismatch stops the release rather than being noted in passing.
    """


class SchemaUnavailableError(LiveTestError):
    """The schema, or the validator, is not available to check a document.

    Writing evidence that was never validated is worse than refusing to write
    it: the artefact would look identical to a checked one.
    """


class DocumentInvalidError(LiveTestError):
    """A document does not satisfy its schema."""


def canonical_json(document: Mapping[str, Any] | Sequence[Any]) -> str:
    """The one serialisation used for every artefact and every digest."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, *, limit: int = MAX_ARTEFACT_BYTES) -> tuple[str, int]:
    """Hash a file in bounded chunks. Returns ``(hexdigest, size_bytes)``.

    Raises:
        LiveTestError: if the file cannot be read, or exceeds *limit* while
            being read. The cap is enforced during the read rather than from a
            stat beforehand, so a file still being appended to cannot slip past.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise LiveTestError(f"{path.name}: exceeds the {limit} byte artefact cap")
                digest.update(chunk)
    except OSError as exc:
        raise LiveTestError(f"{path.name}: cannot be read: {exc}") from exc
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class ArtefactDigest:
    """One file in the evidence tree, identified by content."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> JsonDict:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class EvidenceLayout:
    """Every path under the evidence root, in one place.

    Commands ask this for paths rather than joining strings, so ``collect`` and
    ``finalize`` cannot disagree about where a log was put.
    """

    root: Path

    @property
    def schema_dir(self) -> Path:
        return self.root / SCHEMA_DIR_NAME

    @property
    def result_schema(self) -> Path:
        return self.schema_dir / RESULT_SCHEMA_NAME

    @property
    def manifest_schema(self) -> Path:
        return self.schema_dir / MANIFEST_SCHEMA_NAME

    @property
    def prepare_path(self) -> Path:
        return self.root / PREPARE_NAME

    def scenario_dir(self, scenario_id: str) -> Path:
        return self.root / scenario_id

    def result_path(self, scenario_id: str) -> Path:
        """The result that carries the verdict, and the one the manifest hashes."""
        return self.scenario_dir(scenario_id) / RESULT_NAME

    def attempts_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / ATTEMPTS_DIR_NAME

    def attempt_result_path(self, scenario_id: str, attempt: int) -> Path:
        """One attempt's own result file.

        Attempts get their own files because ``result.json`` alone could not
        survive a re-run: overwriting it would break the digest recorded for the
        earlier attempt, and a broken digest is indistinguishable from tampering.
        """
        return self.attempts_dir(scenario_id) / f"result.{attempt:03d}.json"

    def state_path(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / STATE_NAME

    def collected_path(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / COLLECTED_NAME

    def logs_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / LOGS_DIR_NAME

    def journals_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / JOURNALS_DIR_NAME

    def snapshots_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / SNAPSHOTS_DIR_NAME

    def screenshots_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / SCREENSHOTS_DIR_NAME

    def relative(self, path: Path) -> str:
        """A path as it appears in the manifest: POSIX, relative to the root.

        Falls back to the absolute path when *path* is outside the tree, which
        is worth seeing in a manifest rather than hiding behind an exception.
        """
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def ensure_tree(self, scenario_ids: Sequence[str]) -> tuple[Path, ...]:
        """Create the directories and their ``.gitkeep`` markers.

        Idempotent, and it never removes anything: ``prepare`` is run again
        after a failed session, and a prepare that cleaned the tree would delete
        the evidence from the run that failed.
        """
        created: list[Path] = []
        for directory in (self.root, self.schema_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for scenario_id in scenario_ids:
            base = self.scenario_dir(scenario_id)
            for directory in (base, *(base / name for name in _SCENARIO_SUBDIRS)):
                directory.mkdir(parents=True, exist_ok=True)
                marker = directory / GITKEEP_NAME
                if not marker.exists():
                    marker.write_text("", encoding="utf-8")
                    created.append(marker)
        return tuple(created)


def write_document(
    path: Path, document: Mapping[str, Any], *, schema: Path | None
) -> ArtefactDigest:
    """Validate then write, and return the digest of what was written.

    The order matters: an invalid document is never written, so a partially
    formed artefact cannot be left behind for ``finalize`` to hash and bless.
    """
    if schema is not None:
        validate_document(document, schema)
    text = canonical_json(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise LiveTestError(f"{path.name}: cannot be written: {exc}") from exc
    return ArtefactDigest(path=path.name, sha256=sha256_text(text), size_bytes=len(text.encode()))


def read_document(path: Path, *, expected_sha256: str | None = None) -> JsonDict:
    """Read a JSON artefact, verifying its digest when one is known.

    Raises:
        TamperError: when *expected_sha256* is given and does not match the
            bytes on disk. An operator who edits a result to turn a FAIL into a
            PASS has produced nothing, and this is where that is noticed.
        LiveTestError: when the file is missing, oversized or not JSON.
    """
    if not path.is_file():
        raise LiveTestError(f"{path}: does not exist")
    actual, size = sha256_file(path)
    if expected_sha256 is not None and actual != expected_sha256:
        raise TamperError(
            f"{path}: content hash {actual} does not match the recorded "
            f"{expected_sha256}. This file was modified after it was written; "
            "an edited result is evidence of nothing. Re-run the scenario."
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise LiveTestError(f"{path.name}: cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LiveTestError(
            f"{path.name}: is not valid JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise LiveTestError(f"{path.name}: must be a JSON object, got {type(document).__name__}")
    if size == 0:
        raise LiveTestError(f"{path.name}: is empty")
    return document


def validate_document(document: Mapping[str, Any], schema_path: Path) -> None:
    """Check *document* against a Draft 2020-12 schema.

    Raises:
        SchemaUnavailableError: when the schema file or ``jsonschema`` is
            absent. Live-test runs from a source checkout with the ``dev``
            extra installed; writing an unvalidated artefact instead would
            produce a file indistinguishable from a checked one.
        DocumentInvalidError: naming the failing path and the rule.
    """
    try:
        # Deferred: jsonschema is a dev extra, and its absence has to read as a
        # refusal with an instruction rather than an ImportError from a command.
        from jsonschema import Draft202012Validator  # noqa: PLC0415
    except ImportError as exc:
        raise SchemaUnavailableError(
            "jsonschema is required to write live-test evidence; install the 'dev' extra"
        ) from exc

    if not schema_path.is_file():
        raise SchemaUnavailableError(
            f"{schema_path} is missing. Run live-test from a checkout of the repository: "
            "the evidence schemas are part of it."
        )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaUnavailableError(f"{schema_path.name}: cannot be read: {exc}") from exc

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.absolute_path) or "(root)"
        raise DocumentInvalidError(
            f"{schema_path.name}: {len(errors)} violation(s); first at {where}: {first.message}"
        )


@dataclass(frozen=True, slots=True)
class CollectedFile:
    """One file copied into a scenario directory."""

    source: str
    destination: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> JsonDict:
        return {
            "source": self.source,
            "destination": self.destination,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What one ``collect`` run copied, and what it refused to."""

    scenario_id: str
    copied: tuple[CollectedFile, ...]
    skipped: tuple[str, ...]

    def to_dict(self) -> JsonDict:
        return {
            "scenario_id": self.scenario_id,
            "copied": [entry.to_dict() for entry in self.copied],
            "skipped": list(self.skipped),
        }


def collect_files(
    sources: Sequence[tuple[Path, Path]],
    *,
    scenario_id: str,
    limit: int = MAX_COLLECTED_FILES,
) -> CollectionReport:
    """Copy ``(source, destination)`` pairs, hashing each as it lands.

    A source that is absent, unreadable or oversized is *skipped by name*
    rather than dropped: "console.txt was not where it should be" is the single
    most useful line in a failed collection, and a silent skip would leave the
    operator to notice the gap at finalize time instead.
    """
    copied: list[CollectedFile] = []
    skipped: list[str] = []
    for source, destination in sources:
        if len(copied) >= limit:
            skipped.append(f"{source.name}: the {limit} file collection cap was reached")
            continue
        if not source.is_file():
            skipped.append(f"{source}: not found")
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        except OSError as exc:
            skipped.append(f"{source}: could not be copied: {exc}")
            continue
        try:
            digest, size = sha256_file(destination)
        except LiveTestError as exc:
            destination.unlink(missing_ok=True)
            skipped.append(str(exc))
            continue
        copied.append(
            CollectedFile(
                source=str(source),
                destination=destination.name,
                sha256=digest,
                size_bytes=size,
            )
        )
    return CollectionReport(scenario_id=scenario_id, copied=tuple(copied), skipped=tuple(skipped))


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One artefact the manifest accounts for.

    ``present`` is carried rather than implied by absence from the list,
    because the manifest has to be able to state that something required was
    missing without simply not mentioning it.
    """

    scenario_id: str
    kind: str
    path: str
    required: bool
    present: bool
    sha256: str = ""
    size_bytes: int = 0
    problem: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "path": self.path,
            "required": self.required,
            "present": self.present,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "problem": self.problem,
        }


def digest_entry(
    layout: EvidenceLayout,
    *,
    scenario_id: str,
    kind: str,
    path: Path,
    required: bool,
) -> ManifestEntry:
    """Hash one artefact into a manifest entry, recording why if it could not be."""
    relative = layout.relative(path)
    if not path.is_file():
        return ManifestEntry(
            scenario_id=scenario_id,
            kind=kind,
            path=relative,
            required=required,
            present=False,
            problem="missing",
        )
    try:
        digest, size = sha256_file(path)
    except LiveTestError as exc:
        return ManifestEntry(
            scenario_id=scenario_id,
            kind=kind,
            path=relative,
            required=required,
            present=False,
            problem=str(exc),
        )
    if size == 0:
        # An empty log is not evidence that nothing went wrong; it is evidence
        # that nothing was collected. Required artefacts must have content.
        return ManifestEntry(
            scenario_id=scenario_id,
            kind=kind,
            path=relative,
            required=required,
            present=False,
            sha256=digest,
            size_bytes=0,
            problem="empty",
        )
    return ManifestEntry(
        scenario_id=scenario_id,
        kind=kind,
        path=relative,
        required=required,
        present=True,
        sha256=digest,
        size_bytes=size,
    )
