"""The item-detail seam, checked mechanically instead of one field at a time.

``test_gates_without_producers.py`` is a ledger of dead gates found one by one.
Three of its eight rows turned out to be the *same* root: the mod and the sidecar
spelling one fact differently across a hand-written contract with nothing binding
them. ``schemas/observation.schema.json`` declares ``food`` and its siblings as
objects and constrains none of their properties, so nothing has ever compared the
two vocabularies.

This does. It reads the keys the mod's item readers emit, reads the keys the
sidecar's typed views ask for, and pins the disagreement to what is already known
and recorded. A new mismatch on either side fails here rather than becoming the
ninth thing somebody finds by accident.

**This is a ledger too, not a demand for parity.** A key the mod sends and nobody
reads is ordinary — the reader is free to ignore what it does not need. The
expensive direction is the other one: a key the sidecar *decides* on that the mod
never sends, which reads as the type's default for ever. ``FoodView.is_rotten``
asks whether ``freshness == "rotten"``, the mod sends ``rotten`` as a boolean, and
the answer has always been "no". The counts below are those measured by hand in
``docs/LIMITATIONS.md``; if this file and that section ever disagree, one of them
is out of date and this is the one that ran.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
OBSERVE_LUA: Final = (
    REPO_ROOT / "pz-mod" / "42" / "media" / "lua" / "client" / "PZAgent" / "Observe.lua"
)
POLICY: Final = REPO_ROOT / "packages" / "pz_agent_core" / "src" / "pz_agent_core" / "policy"

#: block -> (the mod reader that builds it, the sidecar module whose typed view
#: reads it, and the counts measured when this was written).
DOMAINS: Final = {
    "food": ("itemFood", "food.py", 22, 8, 6),
    "literature": ("itemLiterature", "literature.py", 11, 6, 6),
    "fluid": ("itemFluid", "drink.py", 16, 3, 1),
}

#: Keys the sidecar reads that the mod does not send. Recorded, not tolerated
#: silently. All but one read as their type's default for ever, which is the
#: defect: a fact nobody measured, stated confidently.
#:
#: The exception is ``literature.unread_recipes``, and it is what the repair of
#: one of these looks like short of writing the missing producer. The key is
#: still unsent -- that is why it is still listed -- but the sidecar no longer
#: decides on it: absent reads as *unknown*, a recipe goal refuses with a
#: sentence saying the count could not be read rather than that the magazine
#: holds nothing new, and a boredom goal scores it on the factors that *were*
#: readable. Membership here means "read without a producer", not "read as a
#: default"; the two stopped being the same thing with that fix.
#:
#: See docs/LIMITATIONS.md, "The item-detail tier speaks two vocabularies".
UNSENT: Final = {
    "food": {
        "boredom_change",
        "burn_progress",
        "destroyed",
        "edible",
        "freshness",
        "frozen",
        "poison_power",
        "raw",
        "raw_unsafe",
        "remaining_portions",
        "required_tool",
        "requires_cooking",
        "rot_progress",
        "tainted",
        "total_portions",
        "unhappy_change",
    },
    # Four keys left this set when the crafting wave landed, and they left the
    # right way: `pages_total`, `min_level` and `max_level` were the mod
    # spelling the same facts as `pages`, `skill_level_min` and
    # `skill_level_max`, and it now spells them the way the sidecar reads them;
    # `unread_recipes` gained a real producer rather than a rename. This block
    # is the one of the three whose vocabularies now agree on every key the mod
    # sends -- 6 of 6 -- so it is also the worked example for repairing `food`
    # and `fluid`.
    "literature": {
        "already_read",
        "boredom_change",
        "destroyed",
        "kind",
        "unhappy_change",
    },
    "fluid": {
        "alcohol_units",
        "alcoholic",
        "capacity_units",
        "destroyed",
        "drinkable",
        "freshness",
        "frozen",
        "poison_power",
        "poisonous",
        "remaining_units",
        "rot_progress",
        "thirst_change",
        "type",
        "unhappy_change",
        "water",
    },
}


def _mod_keys(reader: str) -> set[str]:
    """The keys one of the mod's item readers puts in its returned table.

    The parameter list is matched loosely past the item, because a reader that
    needs something the item cannot answer for itself takes it as a second
    argument — ``itemLiterature(item, isKnown)`` needs the character to say
    which recipes are already known. Pinning the signature to exactly ``(item)``
    made this file fail with "no longer a local function taking one item" for a
    reader that was still right there, still returning a table: the same blind
    spot ``_sidecar_keys`` had, where the seam growing a capability reads as the
    seam disappearing.
    """
    source = OBSERVE_LUA.read_text(encoding="utf-8")
    match = re.search(rf"local function {reader}\(item[^)]*\)(.*?)\nend", source, re.S)
    assert match is not None, f"{reader} is no longer a local function taking an item"
    return set(re.findall(r"^\s{4}(\w+) =", match.group(1), re.M))


def _sidecar_keys(module: str) -> set[str]:
    """The keys one typed view asks its raw payload for.

    Two spellings, because a field that must tell *absent* from *zero* cannot go
    through the shared ``read_*`` helpers at all -- their whole job is to
    substitute a default -- and reads its key with ``payload.get`` instead. The
    first version of this pattern saw only the helper form, so the literature
    block appeared to drop from eleven keys to ten the moment ``unread_recipes``
    became tri-state, when nothing had stopped being read. An extractor that
    goes blind exactly where the seam gets *more* careful would retire this
    file's rows one honest fix at a time.
    """
    source = (POLICY / module).read_text(encoding="utf-8")
    return set(re.findall(r'(?:payload,\s*|payload\.get\()"(\w+)"', source))


def test_both_extractors_see_something() -> None:
    """The positive control.

    Every assertion below compares two sets. An extractor that silently returned
    nothing would make the comparisons pass or fail for reasons having nothing to
    do with the seam, so each side has to demonstrably see real keys first.
    """
    for block, (reader, module, _reads, _sends, overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        assert mod, f"{block}: read no keys out of {reader}"
        assert side, f"{block}: read no keys out of {module}"
        assert mod & side, (
            f"{block}: the two vocabularies now share nothing at all, which means "
            f"an extractor broke rather than that the seam got worse"
        )
        assert len(mod & side) == overlap, (
            f"{block}: {len(mod & side)} keys agree, {overlap} did when this was "
            f"measured — update this file and docs/LIMITATIONS.md together"
        )


def test_the_measured_counts_still_hold() -> None:
    """The numbers docs/LIMITATIONS.md quotes, re-derived rather than trusted."""
    for block, (reader, module, reads, sends, _overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        assert len(side) == reads, f"{block}: sidecar now reads {len(side)}, not {reads}"
        assert len(mod) == sends, f"{block}: mod now sends {len(mod)}, not {sends}"


def test_no_new_key_is_decided_on_without_a_producer() -> None:
    """The one that earns this file.

    A key the sidecar reads and the mod never sends is a fact nobody measured,
    and by default it is also a fact stated confidently. The set is recorded
    above; anything new in it is a fresh instance of the defect this seam keeps
    producing, and anything that has *left* it is a producer somebody wrote —
    either way the ledger and LIMITATIONS.md need the edit before this passes
    again.

    A row can also be repaired without leaving: making the read tri-state keeps
    the key here (nothing produces it) while removing the confident claim, which
    is what happened to ``literature.unread_recipes``. So this test pins which
    keys have no producer, and the comment on the set above carries which of
    them still decide on a default — the second question is the one that hurts,
    and it is not answerable from set membership alone.
    """
    for block, (reader, module, _reads, _sends, _overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        unsent = side - mod
        assert unsent == UNSENT[block], (
            f"{block}: the keys decided on without a producer have changed.\n"
            f"  new (sidecar reads, mod does not send): {sorted(unsent - UNSENT[block])}\n"
            f"  gone (a producer now exists, or the read was dropped): "
            f"{sorted(UNSENT[block] - unsent)}"
        )


# --------------------------------------------------------------------------
# the player's stats map, checked the same way — and clean
# --------------------------------------------------------------------------

#: Stats the mod sends that nothing reads. This is the harmless direction: a
#: reader is free to ignore what it does not need. ``weapon_condition`` is the
#: exception worth knowing about, and it is not carelessness on either side --
#: ``Observe.playerStats`` puts the equipped weapon's wear here *deliberately*,
#: saying so in a comment ("carried under the open stats map because the item
#: tier has no condition field in the schema"), while combat/policy.py looks for
#: it in ``item.extra["weapon"]``. One bridge that was never built, not a
#: vocabulary drifting apart. See the ledger row in
#: test_gates_without_producers.py.
STATS_SENT_UNREAD: Final = {"stress", "weapon_condition", "weapon_condition_max"}


def _mod_stat_keys() -> set[str]:
    source = OBSERVE_LUA.read_text(encoding="utf-8")
    match = re.search(
        r"function Observe\.playerStats\(player\)(.*?)\n  return stats\nend", source, re.S
    )
    assert match is not None, "Observe.playerStats no longer ends by returning stats"
    return set(re.findall(r"stats\.(\w+)\s*=", match.group(1)))


def _sidecar_stat_keys() -> set[str]:
    """Every stat name the sidecar asks the open stats map for."""
    root = REPO_ROOT / "packages" / "pz_agent_core" / "src" / "pz_agent_core"
    keys: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r'_stat\(\s*(?:self|observation)\s*,\s*"([a-z_.]+)"', text))
        keys |= set(re.findall(r'_stat\("([a-z_.]+)"', text))
        keys |= set(re.findall(r'stats\.get\("([a-z_.]+)"', text))
    return keys


def test_no_stat_is_decided_on_without_a_producer() -> None:
    """The stats map is clean in the direction that costs something.

    This was expected to be the item blocks over again and is not, which is
    worth a test rather than a shrug: every stat the sidecar reads --
    endurance, fatigue, health, hunger, panic, thirst -- is one
    ``Observe.playerStats`` sends, and ``observe.wounds_unknown`` is minted by
    ObserveModel's limit block. Nothing here reads as a default for ever. The
    disease is confined to the item-detail blocks above, and this asserts that
    it stays confined.
    """
    mod, side = _mod_stat_keys(), _sidecar_stat_keys()
    assert mod, "read no keys out of Observe.playerStats"
    assert side, "read no stat names out of the sidecar"
    namespaced = {key for key in side if "." in key}
    for key in namespaced:
        assert key.startswith("observe."), f"{key}: an unexpected namespace in the stats map"
    unsent = side - mod - namespaced
    assert unsent == set(), (
        f"a stat is now decided on without a producer: {sorted(unsent)} -- either "
        f"the mod stopped sending it or the sidecar started reading a name nobody "
        f"writes, and both read as the accessor's default for ever"
    )


def test_the_stats_nobody_reads_are_the_known_ones() -> None:
    """The harmless direction, pinned so a new one gets a look rather than a pass."""
    assert _mod_stat_keys() - _sidecar_stat_keys() == STATS_SENT_UNREAD


# --------------------------------------------------------------------------
# the structural tiers, which turn out to be exact
# --------------------------------------------------------------------------

#: Each structural block, as (a slice of ObserveModel that builds it, the
#: sidecar dataclass that reads it). These are the tiers with a typed dataclass
#: on one side and an explicit table on the other, and every one of them agrees
#: key for key.
STRUCTURAL: Final = {
    "item": (r"local item = \{(.*?)\n  return item", "ItemView", 12),
    "container": (
        r"state\.containers\[state\.containerCount\] = \{(.*?)\n  \}",
        "ContainerView",
        7,
    ),
    "zombie": (
        r"local zombie = \{ ref = ref, distance = distance \}(.*?)\n  return",
        "NearbyZombie",
        6,
    ),
}

#: Keys a structural tier sends that its typed dataclass does not name, because
#: they ride ``ItemView.extra`` instead. Not a drift to be closed by adding a
#: field: this is the *fourth* free-form block, and it arrived with the crafting
#: wave. The other three -- ``food``, ``literature``, ``fluid`` -- are exactly
#: where the two sides came to spell the same facts differently, because nothing
#: about a ``JsonDict`` forces agreement, and ``crafting`` is now open to the
#: same thing: the mod fills it in ``Observe.attachRecipes``, the sidecar reads
#: it back through ``item.extra["crafting"]`` in ``policy/crafting.py`` and
#: ``observation/compact.py``, and no test compares those two vocabularies.
#: Listed here so the exactness claim below stays honest about what it covers.
STRUCTURAL_FREEFORM: Final = {"item": {"crafting"}}

MODEL_LUA: Final = (
    REPO_ROOT / "pz-mod" / "42" / "media" / "lua" / "shared" / "PZAgent" / "ObserveModel.lua"
)
MESSAGES_PY: Final = (
    REPO_ROOT / "packages" / "pz_agent_core" / "src" / "pz_agent_core" / "protocol" / "messages.py"
)


def _structural_mod_keys(block: str, pattern: str) -> set[str]:
    source = MODEL_LUA.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.S)
    assert match is not None, f"{block}: ObserveModel no longer builds this table the same way"
    body = match.group(1)
    keys = set(re.findall(r"^\s+(\w+) =", body, re.M))
    keys |= set(re.findall(r"^\s*(?:item|zombie)\.(\w+)\s*=", body, re.M))
    if block == "zombie":
        keys |= {"ref", "distance"}
    return keys


def _structural_sidecar_keys(cls: str) -> set[str]:
    source = MESSAGES_PY.read_text(encoding="utf-8")
    match = re.search(rf"class {cls}.*?(?=\nclass |\Z)", source, re.S)
    assert match is not None, f"{cls} is no longer a class in messages.py"
    return set(re.findall(r'payload(?:\.get\(|,\s*)"(\w+)"', match.group(0)))


def test_the_structural_tiers_agree_key_for_key() -> None:
    """The finding that explains the ones above.

    The item's own fields, the container's, and the zombie's all match exactly —
    12, 7 and 6 keys, nothing read that is not sent and nothing sent that is not
    read. That is not luck: each of these has a typed dataclass on the Python
    side and an explicit table on the Lua side, and the two were written against
    each other. The blocks that diverged are the ones passed through as raw
    ``JsonDict`` where nothing forced agreement.

    So the repair for the item-detail tier is not "rewrite the contract" — it is
    to give ``food``, ``literature`` and ``fluid`` the treatment these three
    already have. Asserting the exactness keeps that argument true.
    """
    for block, (pattern, cls, expected) in STRUCTURAL.items():
        mod = _structural_mod_keys(block, pattern) - STRUCTURAL_FREEFORM.get(block, set())
        side = _structural_sidecar_keys(cls)
        assert mod, f"{block}: extracted no keys from the mod"
        assert side, f"{block}: extracted no keys from {cls}"
        assert len(mod) == expected, (
            f"{block}: mod now sends {len(mod)} typed keys, not {expected} "
            f"(free-form keys excluded: {sorted(STRUCTURAL_FREEFORM.get(block, set()))})"
        )
        assert mod == side, (
            f"{block}: the structural tier has drifted.\n"
            f"  read but not sent: {sorted(side - mod)}\n"
            f"  sent but not read: {sorted(mod - side)}\n"
            f"This tier used to agree exactly, which is the argument that the "
            f"item-detail divergence is a fixable local defect rather than the "
            f"seam's natural state."
        )
