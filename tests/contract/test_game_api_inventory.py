"""Every engine class the mod names must appear in the unconfirmed-API inventory.

``docs/GAME_API_VERIFICATION.md`` is the document a person with the game works
through. It is the only complete statement of what this project is guessing
about Build 42.20, and until now nothing checked that it was complete.

Five documents told an operator that ``grep -rn "Build 42:" pz-mod/`` "lists
every guess". It returns six lines in two files, against 52 rows marked
``requires_live`` — roughly an eighth. The prompt the local agent starts from
said "Это исчерпывающий список", so an agent following it would have enumerated
six places and believed the unconfirmed surface covered.

Correcting the sentences is not enough on its own: the inventory can still fall
behind the code, and a document that is *believed* complete and is not is worse
than one nobody trusts. So this checks the direction that matters — an engine
class the mod constructs or requires, and the document does not mention, is a
guess nobody was told to verify.

The reverse direction is deliberately not checked. The document lists Java
accessors and methods (``Stats.getThirst``, ``IsoGridSquare.getObjects``) that
appear in Lua only as string arguments to reflective helpers, so requiring every
documented row to appear in the mod's source would fail on rows that are
correctly there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INVENTORY: Final = REPO_ROOT / "docs" / "GAME_API_VERIFICATION.md"
MOD_ROOT: Final = REPO_ROOT / "pz-mod"

#: ``Toolkit.construct("ISEatFoodAction", …)`` — the mod instantiating a vanilla
#: timed action. Every one of these is an assumption about a constructor.
_CONSTRUCT: Final = re.compile(r'construct\(\s*"([A-Za-z][A-Za-z0-9_]*)"')

#: A quoted global the mod probes for, as it appears in a ``requires`` list or a
#: ``requireSymbols`` call: ``"ISWalkToTimedAction"`` or
#: ``"ISTimedActionQueue.add"``. Only names starting with a capital are engine
#: symbols; the mod's own tables are lowercase.
_SYMBOL: Final = re.compile(r'"([A-Z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?)"')

#: Quoted capitalised strings that are not engine symbols. Listed rather than
#: pattern-matched, so a real symbol can never be excused by a clever rule.
_NOT_A_SYMBOL: Final = frozenset(
    {
        "OFF",
        "OBSERVE",
        "ASSISTED",
        "AUTONOMOUS",
        "REFLEX_ONLY",
        "EXPERIMENTAL_INPUT",
        "PZAgent",
        "MOD",
        "NONE",
        "MANUAL",
        "AMBIGUOUS",
    }
)


def _mod_sources() -> list[Path]:
    found = sorted(MOD_ROOT.rglob("*.lua"))
    assert len(found) >= 20, f"only {len(found)} Lua files found; the glob has broken"
    return found


def _named_by_the_mod() -> dict[str, list[str]]:
    """Every engine class the mod constructs or probes for, and where."""
    found: dict[str, list[str]] = {}
    for path in _mod_sources():
        text = path.read_text(encoding="utf-8")
        names = set(_CONSTRUCT.findall(text))
        for symbol in _SYMBOL.findall(text):
            head = symbol.split(".", 1)[0]
            if head in _NOT_A_SYMBOL or not head.startswith("IS"):
                continue
            names.add(head)
        for name in names:
            found.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    return found


def test_the_inventory_names_every_engine_class_the_mod_uses() -> None:
    """A class the mod builds and the document omits is an unlisted guess."""
    document = INVENTORY.read_text(encoding="utf-8")
    named = _named_by_the_mod()
    assert named, "no engine classes were found in the mod, so this proves nothing"

    # Word-boundary, not substring: a first attempt used ``name in document``
    # and a mutation renaming ``ISEatFoodAction`` to ``ISEatFoodActionXX`` in the
    # table left the test green, because the old name is a prefix of the new one.
    missing = {
        name: sorted(set(where))
        for name, where in named.items()
        if re.search(rf"\b{re.escape(name)}\b", document) is None
    }
    assert missing == {}, (
        "these engine classes are used by the mod and are not in "
        "docs/GAME_API_VERIFICATION.md, so nobody has been told to verify them"
    )


def test_the_inventory_is_larger_than_the_grep_it_used_to_be_confused_with() -> None:
    """The claim that replaced "the grep lists every guess" must stay true.

    Five documents now say the grep is a shortcut and the table is the list. If
    the table ever shrinks to the size of the grep, those sentences become wrong
    again — quietly, and in the prompt the local agent starts from.
    """
    document = INVENTORY.read_text(encoding="utf-8")
    rows = document.count("`requires_live`")

    commented = sum(path.read_text(encoding="utf-8").count("Build 42:") for path in _mod_sources())

    assert rows > commented * 2, (
        f"the inventory has {rows} unconfirmed rows against {commented} 'Build 42:' "
        "comments; the documents claim the table is substantially the larger list"
    )
