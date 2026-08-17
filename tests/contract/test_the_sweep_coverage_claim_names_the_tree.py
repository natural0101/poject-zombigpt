"""A coverage claim is worth exactly the set it was measured over.

``docs/PROGRESS.md`` records which parts of the shipped code the refusal-plant
sweep has been run against. The first version of that record said the sweep had
covered "every area of shipped code", listing nine. There are twenty-one: the
whole of ``pz_agent_mcp`` and eleven more subpackages inside the core had never
been assigned to an agent, so they had never been measured and the sentence read
as though they had.

That is the failure this repository keeps meeting in other forms — a guard drawn
around the files where a defect was caught rather than around the fact, a count
typed once and left to drift. The fix is always the same: derive the set, and
say what is excluded and why. So the set here is derived from the tree, and the
document has to name every member, whichever of the two lists it puts it in.

**What this does not check.** It cannot tell "swept" from "not swept" — a name
in either list satisfies it, because which list a package belongs in is a fact
about work done, not about the tree, and a test that tried to decide it would be
asserting the document against itself. What it catches is the case that actually
happened: a package the paragraph does not mention at all, so a reader counts
nine and the tree holds twenty-one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PROGRESS: Final = REPO_ROOT / "docs" / "PROGRESS.md"

#: The paragraph pair that records what the sweep has and has not been run over.
#: Anchored on the sentence that states the rule rather than on a heading,
#: because the section around it is prose that gets rewritten.
CLAIM_ANCHOR: Final = "every area it was *given*"

#: Directories that are not shipped subpackages.
NOT_A_SUBPACKAGE: Final = frozenset({"__pycache__", "tests", "py.typed"})


def _shipped_units() -> set[str]:
    """Every unit of shipped code the sweep could be run against.

    Derived, not listed. Two kinds, because the tree has two kinds: the
    importable subpackages under ``packages/*/src/<package>/``, and the Lua mod
    under ``pz-mod/``.

    The mod is here because leaving it out is the mistake this file exists to
    catch, made a second time. The first version derived only from
    ``packages/``, so it happily passed a claim that named every Python
    subpackage and said nothing at all about the twenty-odd Lua modules that run
    inside the game — a guard exactly as wide as its own set, asked a question
    wider than that. It is named as one unit rather than module by module: the
    claim this checks is "which areas has the sweep been run against", and the
    mod is one area in that sense, while listing twenty file names in a
    paragraph would be noise nobody reads.
    """
    found: set[str] = set()
    for source in (REPO_ROOT / "packages").glob("*/src/*/"):
        if not source.is_dir():
            continue
        found.add(source.name)
        for child in source.iterdir():
            if child.is_dir() and child.name not in NOT_A_SUBPACKAGE:
                found.add(child.name)
    if any((REPO_ROOT / "pz-mod").glob("*/media/lua")):
        found.add("pz-mod")
    return found


def _shipped_subpackages() -> set[str]:
    """Kept as the old name for the derivation above."""
    return _shipped_units()


def _claim() -> str:
    text = PROGRESS.read_text(encoding="utf-8")
    start = text.find(CLAIM_ANCHOR)
    assert start != -1, (
        f"{PROGRESS.name} no longer contains the sweep's coverage claim "
        f"(anchored on {CLAIM_ANCHOR!r}); if it moved, move this check with it"
    )
    body = text[start:]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def test_the_derivation_finds_the_tree() -> None:
    """A glob that matched nothing would make the check below vacuous."""
    found = _shipped_subpackages()

    assert len(found) > 15, f"only {len(found)} unit(s) found; the tree is larger"
    assert {"pz_agent_mcp", "safety", "ipc"} <= found
    assert "pz-mod" in found, (
        "the Lua mod is not in the derived set, so a claim that forgot the half "
        "that runs inside the game would pass — which is how it passed once already"
    )


def test_every_shipped_subpackage_is_named_in_the_coverage_claim() -> None:
    """Named in one list or the other — the omission is what this catches."""
    claim = _claim()
    unmentioned = sorted(name for name in _shipped_subpackages() if name not in claim)

    assert not unmentioned, (
        f"the sweep's coverage claim in {PROGRESS.name} says nothing about "
        f"{unmentioned}. A reader counts the names it does list and takes that for "
        "the tree; that is exactly how the claim came to say 'every area of shipped "
        "code' while a whole package had never been measured. Put each one in the "
        "swept list or in the not-yet list, whichever is true"
    )
