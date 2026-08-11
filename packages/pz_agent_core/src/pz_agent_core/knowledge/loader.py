"""Load the gameplay knowledge corpus, refusing what it cannot vouch for.

:func:`load_corpus` walks ``knowledge/gameplay/*.yaml`` under a repository
root and returns a :class:`KnowledgeCorpus`, or raises one :class:`CorpusError`
naming the file, the refusal code and — when the offending rule's id passed
the schema's own id pattern — the rule id. Never the file's free text: a
corpus document is data, and a refusal is not a place to launder it into
output.

The gates, in the order they run:

* **Bounded before parsed.** At most :data:`MAX_DOCUMENTS` files, each at most
  :data:`MAX_FILE_BYTES` on disk — the size is refused off ``stat`` before a
  byte is read, so an oversized file cannot buy a parse.
* **Schema shape.** Every document goes through
  :meth:`~pz_agent_core.knowledge.model.KnowledgeDocument.from_mapping`, which
  mirrors ``schemas/gameplay-knowledge.schema.json`` field for field, closed
  enums included.
* **Honesty against the tree.** A ``verified_script`` rule's ``proven_by``
  test paths must exist on disk under the root — a deleted test is a loud
  :attr:`~pz_agent_core.knowledge.model.KnowledgeRefusal.DEAD_PROOF_PATH`
  refusal at load, never a silently kept claim. A code-sourced rule's
  ``source_detail`` repo path must exist the same way.
* **One corpus, one id space.** A rule id appearing twice anywhere in the
  corpus is refused naming both files.

PyYAML is a dev-extra dependency (``pyproject.toml``), not a runtime one: the
corpus is read by the sidecar's planner tooling and by tests, never on the
game-critical path, and the import is deferred into :func:`load_corpus` so
importing :mod:`pz_agent_core` keeps costing nothing. Calling the loader
without PyYAML installed is a typed refusal rather than an ImportError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

from .model import (
    KnowledgeDocument,
    KnowledgeRefusal,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeValidationError,
)

__all__ = [
    "GAMEPLAY_SUBDIR",
    "MAX_DOCUMENTS",
    "MAX_FILE_BYTES",
    "CorpusError",
    "KnowledgeCorpus",
    "load_corpus",
]

#: Documents one corpus may hold. The domain enum has 14 members and one file
#: per domain is the intended shape; 32 leaves room without being unbounded.
MAX_DOCUMENTS: Final = 32

#: Bytes one document may occupy on disk, refused off ``stat`` before parsing.
#: 64 rules of a few hundred bounded characters each fit in a fraction of it.
MAX_FILE_BYTES: Final = 256 * 1024

#: Where the corpus lives under the repository root.
GAMEPLAY_SUBDIR: Final = ("knowledge", "gameplay")


class CorpusError(Exception):
    """One typed refusal: which file, which gate, and — when its id passed
    the schema pattern — which rule. The corpus is loaded whole or not at all;
    a partially trusted knowledge base would be worse than none."""

    def __init__(
        self,
        refusal: KnowledgeRefusal,
        message: str,
        *,
        file: str,
        rule_id: str | None = None,
    ) -> None:
        detail = f"{file}: {message}" + (f" (rule {rule_id})" if rule_id else "")
        super().__init__(detail)
        self.refusal = refusal
        self.file = file
        self.rule_id = rule_id


@dataclass(frozen=True, slots=True)
class KnowledgeCorpus:
    """Every document the loader accepted, in filename order."""

    documents: tuple[KnowledgeDocument, ...]

    @property
    def rules(self) -> tuple[KnowledgeRule, ...]:
        """All rules across the corpus, document order preserved."""
        return tuple(rule for document in self.documents for rule in document.rules)

    def rule(self, rule_id: str) -> KnowledgeRule | None:
        """The rule with this id, or None. Ids are unique — the loader saw to it."""
        for document in self.documents:
            for rule in document.rules:
                if rule.id == rule_id:
                    return rule
        return None


def _yaml_module(*, file: str) -> ModuleType:
    """The parser, or a typed refusal naming the missing dev extra."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:
        raise CorpusError(
            KnowledgeRefusal.YAML_UNAVAILABLE,
            "PyYAML is required to read the knowledge corpus; install the 'dev' extra",
            file=file,
        ) from exc
    return yaml


def _relative(path: Path, root: Path) -> str:
    """The path as the refusal names it: repo-relative where possible."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        # Discarding "path is not under root": the name is only for the
        # refusal message, and an absolute spelling there is the smaller loss
        # than refusing to report the real problem over a cosmetic one.
        return path.as_posix()


def _load_document(path: Path, root: Path) -> KnowledgeDocument:
    file = _relative(path, root)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CorpusError(
            KnowledgeRefusal.UNREADABLE_FILE, "the file could not be read", file=file
        ) from exc
    if size > MAX_FILE_BYTES:
        raise CorpusError(
            KnowledgeRefusal.FILE_TOO_LARGE,
            f"{size} bytes on disk, above the {MAX_FILE_BYTES}-byte bound; refused before parsing",
            file=file,
        )
    yaml = _yaml_module(file=file)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CorpusError(
            KnowledgeRefusal.UNREADABLE_FILE, "the file could not be read as UTF-8", file=file
        ) from exc
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # The parser's message quotes document text, so it stays off the wire;
        # the file name is enough to find the breakage.
        raise CorpusError(
            KnowledgeRefusal.PARSE_FAILED, "the file is not parseable YAML", file=file
        ) from exc
    if not isinstance(payload, dict):
        raise CorpusError(
            KnowledgeRefusal.NOT_A_MAPPING,
            "a knowledge document must be a single YAML mapping",
            file=file,
        )
    try:
        return KnowledgeDocument.from_mapping(payload)
    except KnowledgeValidationError as exc:
        raise CorpusError(exc.refusal, str(exc), file=file, rule_id=exc.rule_id) from exc


def _check_paths_exist(rule: KnowledgeRule, root: Path, *, file: str) -> None:
    """The honesty gates that need the tree: named paths must exist under root."""
    if rule.status is KnowledgeStatus.VERIFIED_SCRIPT:
        for proof in rule.proven_by:
            if not (root / proof).exists():
                raise CorpusError(
                    KnowledgeRefusal.DEAD_PROOF_PATH,
                    f"proven_by names {proof}, which does not exist on disk — "
                    "the claim is no longer pinned and may not stay verified_script",
                    file=file,
                    rule_id=rule.id,
                )
    if rule.source is KnowledgeSource.CODE:
        # ``source_detail`` is ``<repo-path>::<symbol>``; the path half must
        # exist, or the rule cites code the tree no longer contains.
        repo_path = rule.source_detail.split("::", 1)[0]
        if not (root / repo_path).exists():
            raise CorpusError(
                KnowledgeRefusal.DEAD_SOURCE_PATH,
                f"source_detail names {repo_path}, which does not exist on disk",
                file=file,
                rule_id=rule.id,
            )


def load_corpus(root: Path) -> KnowledgeCorpus:
    """Load every document under ``<root>/knowledge/gameplay``, or refuse.

    Args:
        root: the repository root. Proof paths and code paths inside the rules
            are resolved against it, which is what makes "the test exists" a
            checkable claim instead of a naming convention.

    Returns:
        The whole corpus. An empty directory (or none) is an empty corpus,
        not an error — a repo without knowledge has none to misstate.

    Raises:
        CorpusError: naming the file, the refusal code and where possible the
            rule id. The first failed gate wins; nothing is partially loaded.
    """
    directory = root.joinpath(*GAMEPLAY_SUBDIR)
    corpus_name = "/".join(GAMEPLAY_SUBDIR)
    if not directory.is_dir():
        return KnowledgeCorpus(documents=())
    files = sorted(path for path in directory.glob("*.yaml") if path.is_file())
    if len(files) > MAX_DOCUMENTS:
        raise CorpusError(
            KnowledgeRefusal.TOO_MANY_DOCUMENTS,
            f"{len(files)} documents, above the {MAX_DOCUMENTS}-document bound",
            file=corpus_name,
        )
    documents: list[KnowledgeDocument] = []
    seen: dict[str, str] = {}
    for path in files:
        file = _relative(path, root)
        document = _load_document(path, root)
        for rule in document.rules:
            first = seen.get(rule.id)
            if first is not None:
                raise CorpusError(
                    KnowledgeRefusal.DUPLICATE_RULE_ID,
                    f"rule id already defined in {first}",
                    file=file,
                    rule_id=rule.id,
                )
            seen[rule.id] = file
            _check_paths_exist(rule, root, file=file)
        documents.append(document)
    return KnowledgeCorpus(documents=tuple(documents))
