"""One document, built by the mod and read by the sidecar, in one process.

The observation seam is the one place in this project where a defect has been
found eight times and never by a test. The reason is structural and is stated in
``test_gates_without_producers.py``: the sidecar's fixtures build the document
the sidecar expects, the mod's suites build the document the mod emits, and
until now nothing put one side's output into the other side's reader. Both sides
are green against their own idea of the contract.

The command direction has had ``test_adapter_args_agreement.py`` for a long
time, which runs the mod's own loader and compares its declarations to what the
sidecar sends. This is that check, pointed the other way.

``tests/lua/support/dump_observation.lua`` builds one observation through
``Observe.playerFields``, ``Observe.inventoryRoots`` and ``ObserveModel.build``
— the shipped code, not a fixture — against fakes that stand in for the *engine*
and answer exactly the accessor names the mod's own readers ask for. Nothing
here writes down what the document should look like. Python then decodes it with
``Observation.from_dict`` and hands the items to the typed views the policies
use.

**What this catches that a key-set comparison cannot.** The item-detail blocks
are raw ``JsonDict`` on both sides, so a disagreement is not a type error, not a
missing field and not a crash — it is a *decision* coming out wrong. The mod
reports a sandwich as rotten, burnt and poisonous; ``FoodView.is_rotten`` asks
whether ``freshness == "rotten"`` and answers no. That is the whole defect
class, demonstrated end to end rather than argued from two files. A regex over
the sources can be fooled by how the producer is written — one row in the gate
ledger was, for four commits. A test that runs the producer cannot be.

The assertions below are therefore written as *the reading the sidecar actually
gets*, with the raw value beside it. When a repair lands, they fail and say so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.policy.food import FoodView
from pz_agent_core.policy.literature import LiteratureView
from pz_agent_core.protocol.messages import ItemView, Observation

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_observation.lua"
_INTERPRETERS: Final = ("lua5.4", "lua")


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip(f"no Lua interpreter on PATH (tried {', '.join(_INTERPRETERS)})")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    """The observation the shipped mod builds, decoded from its own JSON."""
    completed = subprocess.run(
        [_interpreter(), str(DUMPER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the dumper failed, so nothing below is testing the seam:\n{completed.stderr}"
    )
    decoded = json.loads(completed.stdout)
    assert isinstance(decoded, dict)
    return decoded


def _item_named(observation: Observation, display_name: str) -> ItemView:
    inventory = observation.inventory
    assert inventory is not None, "the mod produced no inventory block"
    for item in inventory.items:
        if item.display_name == display_name:
            return item
    raise AssertionError(
        f"no item called {display_name!r} in the document the mod built; "
        f"saw {[i.display_name for i in inventory.items]}"
    )


def test_the_sidecar_can_decode_what_the_mod_builds(document: dict[str, Any]) -> None:
    """The positive control, and it is not a formality.

    If ``Observation.from_dict`` refuses the mod's own document, every other
    assertion in this file is vacuous — and a refusal here would itself be the
    most serious finding the seam could produce.
    """
    observation = Observation.from_dict(document)
    assert observation.inventory is not None
    assert len(observation.inventory.items) == 3, (
        "the dumper puts three items in the character's hands; if this changes, "
        "the assertions below are looking at something else"
    )


def test_a_rotten_meal_reads_as_fresh_to_the_food_policy(document: dict[str, Any]) -> None:
    """The defect, demonstrated rather than argued.

    The mod says ``rotten: true``. ``FoodView`` asks whether ``freshness`` is the
    string ``"rotten"``, the key is absent, ``read_str`` substitutes ``""``, and
    the answer is no. Nothing errors: the two sides simply spell the fact
    differently and the reading comes out confident and wrong.

    ``poisonous`` is asserted beside it because it is the one hazard key that
    *does* cross in this block — the seam is not uniformly broken, and a repair
    that fixed rot by breaking poison would pass a looser test than this one.
    """
    observation = Observation.from_dict(document)
    sandwich = _item_named(observation, "Sandwich")

    raw = sandwich.food
    assert raw is not None, "the mod built no food block for a sandwich it fed hunger from"
    assert raw.get("rotten") is True, "the mod stopped reporting rot; re-derive this file"
    assert raw.get("poisonous") is True

    view = FoodView.from_item(sandwich)
    assert view is not None
    assert view.is_rotten is False, (
        "FoodView.is_rotten is True, which means the vocabulary gap this file "
        "pins has been repaired — update docs/LIMITATIONS.md and the UNSENT set "
        "in test_item_domain_vocabularies.py, then change this assertion"
    )
    assert view.poisonous is True, (
        "poisonous crosses the seam and must keep crossing it: it is the reason "
        "poisoned food is refused at all"
    )


def test_a_books_pages_survive_the_seam(document: dict[str, Any]) -> None:
    """The half of the seam that was repaired, held in place.

    ``pages_total``, ``min_level`` and ``max_level`` used to be sent as
    ``pages``, ``skill_level_min`` and ``skill_level_max`` and read as nothing.
    The crafting wave renamed them on the mod's side. This is what that repair
    looks like from the sidecar's chair, and it fails if the rename is undone.
    """
    observation = Observation.from_dict(document)
    book = _item_named(observation, "Carpentry for Beginners")

    view = LiteratureView.from_item(book)
    assert view is not None
    assert view.pages_total == 220
    assert view.pages_read == 40
    assert view.min_level == 0
    assert view.max_level == 2
    assert view.unread_recipes is None, (
        "absent must stay unknown rather than zero; a real count here means the "
        "mod gained a recipe reader and the tri-state can be revisited"
    )
