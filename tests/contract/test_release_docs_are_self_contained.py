"""A document in the release archive may not point at one that is not.

The archive is what an operator installs on a Windows machine that has no
checkout and, in the middle of a live run, may have no network either. Every
document in it is a promise that the thing it names is reachable.

Two were not. ``docs/LOCAL_GAME_HANDOFF.md`` and ``docs/LIVE_TEST_PLAYBOOK.md``
both ship, and both were edited to say that
``docs/GAME_API_VERIFICATION.md`` — the inventory of all 52 unconfirmed engine
symbols — is the list to work through, replacing an earlier claim that a grep
covered it. That correction pointed at a file ``DOC_NAMES`` did not carry.
``docs/LOCAL_AGENT_PROMPT.md`` was missing for the same reason: nothing checked.

So the fix for one defect introduced another, in the same afternoon, and only
by opening the archive did it show. This is the check that would have caught it
without opening anything.

Scope is deliberately narrow. Only ``docs/*.md`` references are followed —
links to source files, to ``schemas/`` or to the web are not promises this
archive can keep, and demanding it carry the whole repository would make the
check unusable rather than strict.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packaging" / "windows"))

import build_rc  # noqa: E402  (path set up above)

#: ``docs/GAME_API_VERIFICATION.md`` however it is written — bare, in backticks,
#: or as a markdown link target.
_DOC_REFERENCE: Final = re.compile(r"docs/([A-Z][A-Z0-9_]*\.md)")

#: Documents that ship at the archive root rather than under ``docs/``.
_ROOT_DOCS: Final = frozenset(build_rc.META_NAMES)

#: Contributor documents ``README.md`` links to, deliberately absent from an
#: operator's archive: nobody installing on a Windows machine to run twenty
#: scenarios needs the layout of the test suite. Pinned as a literal set rather
#: than matched by a rule, so that a *new* dangling reference fails here instead
#: of being waved through by a pattern someone widened.
_CONTRIBUTOR_ONLY: Final = frozenset(
    {"ARCHITECTURE.md", "DEVELOPMENT.md", "TESTING.md", "PROTOCOL.md"}
)


def _shipped() -> frozenset[str]:
    return frozenset(build_rc.DOC_NAMES)


def _sources() -> list[Path]:
    """The repository copies of the documents the archive carries."""
    found = [REPO_ROOT / "docs" / name for name in build_rc.DOC_NAMES]
    found += [REPO_ROOT / name for name in build_rc.META_NAMES if name.endswith(".md")]
    missing = [path for path in found if not path.is_file()]
    assert missing == [], f"DOC_NAMES/META_NAMES name files that do not exist: {missing}"
    return found


def test_the_shipped_list_is_not_empty() -> None:
    """Every assertion below is a set comparison and would pass over nothing."""
    assert len(build_rc.DOC_NAMES) >= 9, "the shipped document list has shrunk unexpectedly"


def test_no_shipped_document_points_at_one_that_is_not_shipped() -> None:
    shipped = _shipped()
    dangling: dict[str, list[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for target in sorted(set(_DOC_REFERENCE.findall(text))):
            if target in shipped or target in _ROOT_DOCS or target in _CONTRIBUTOR_ONLY:
                continue
            dangling.setdefault(path.name, []).append(target)

    assert dangling == {}, (
        "these shipped documents reference documents the archive does not carry, "
        "so an operator installing from the ZIP cannot follow them"
    )


def test_the_two_documents_the_operator_cannot_work_without_are_shipped() -> None:
    """Named individually because losing either has a specific, known cost.

    Without ``GAME_API_VERIFICATION.md`` there is no list of what is
    unconfirmed, and the handoff's instruction to work through it is empty.
    Without ``LOCAL_AGENT_PROMPT.md`` the local agent has no brief at all.
    """
    shipped = _shipped()
    for required in ("GAME_API_VERIFICATION.md", "LOCAL_AGENT_PROMPT.md"):
        assert required in shipped, f"{required} is not in the release archive"


def test_every_referenced_document_exists_in_the_repository() -> None:
    """A reference can also dangle at the source, which ships a broken link."""
    absent: dict[str, list[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for target in sorted(set(_DOC_REFERENCE.findall(text))):
            if (REPO_ROOT / "docs" / target).is_file():
                continue
            if target in _ROOT_DOCS and (REPO_ROOT / target).is_file():
                continue
            absent.setdefault(path.name, []).append(target)

    assert absent == {}, "these references name documents that do not exist at all"
