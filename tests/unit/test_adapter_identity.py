"""The recogniser every postcondition in the adapter layer runs on.

``find_by_identity`` answers "is this the same object I acted on", and every
adapter that proves something moved, was worn, was eaten or was used asks it:
``TransferAdapter._verify_landed``, ``BatchTransferAdapter.verify`` and
``.progress``, ``EnsureMainAdapter.verify``, ``consume._track``,
``BandageAdapter``'s dressing check. It had no test of its own, and a sweep
found out the expensive way: dropping the generation half of its comparison —
one line, ``and parsed.generation == identity.generation`` — left the whole
suite green.

Generation is in :class:`ItemIdentity` because Project Zomboid reuses runtime
ids, and a bump means a save/load boundary after which equal runtime ids say
nothing. The session tier voids stale references too — a changed save ends the
session outright — so this is the second line rather than the only one, but it
is the line that decides what an *observation* is taken to be, which is what a
postcondition is read from. Without it a reference minted before a save/load
matches whatever object now holds that runtime id, and the adapter reports
success about an object it never touched.

The container tail deliberately does *not* take part: an item's reference is
rebuilt around whatever container holds it, so after a transfer the same object
answers to a different string, and a recogniser that compared whole references
would call every successful move a disappearance. Both halves are asserted
here, because a test for one alone is satisfied by a comparison that is simply
wrong in the other direction.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import pytest

from pz_agent_core.actions.adapters.common import (
    ItemIdentity,
    find_by_identity,
    identity_of,
)
from pz_agent_core.protocol import InventoryView
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import a_world
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    backpack_container,
    container_tail,
    drink_item,
    main_container,
)

#: The same runtime id on either side of a save/load. The mod mints the second
#: one after the bump, and the two are different objects however alike they look.
RUNTIME_ID: Final = "43"


def _ref(*, generation: int, container_ref: str = MAIN_REF) -> str:
    return f"item:{DEFAULT_SESSION}:{container_tail(container_ref)}:{RUNTIME_ID}:{generation}"


def _inventory(*refs: str) -> InventoryView:
    """An observed inventory holding one item per reference given."""
    items = [
        replace(
            drink_item(RUNTIME_ID),
            ref=ref,
            container_ref=BACKPACK_REF if container_tail(BACKPACK_REF) in ref else MAIN_REF,
        )
        for ref in refs
    ]
    observation = a_world(
        items=items,
        containers=[main_container(), backpack_container()],
        stats={"hunger": 0.5, "thirst": 0.5},
    )
    inventory = observation.inventory
    assert inventory is not None
    return inventory


def test_the_same_object_is_found_after_it_moves_container() -> None:
    """The control: without it, a recogniser that never matches would pass below."""
    identity = identity_of(_ref(generation=0))
    inventory = _inventory(_ref(generation=0, container_ref=BACKPACK_REF))

    found = find_by_identity(inventory, identity)

    assert len(found) == 1, "a transfer renamed the reference and the object was lost with it"


def test_a_reference_from_before_a_save_load_matches_nothing_after_it() -> None:
    """The half that had no test, and the reason the field is in the identity."""
    minted_before = identity_of(_ref(generation=0))
    after_the_bump = _inventory(_ref(generation=1))

    assert find_by_identity(after_the_bump, minted_before) == (), (
        "an item reference from before a save/load matched an object that merely "
        "inherited its runtime id, so a postcondition would be read off the wrong item"
    )


def test_a_duplicated_runtime_id_is_reported_as_more_than_one() -> None:
    """How many is the interesting number: a duplication is never a move."""
    identity = ItemIdentity(runtime_id=RUNTIME_ID, generation=0)
    doubled = _inventory(_ref(generation=0), _ref(generation=0, container_ref=BACKPACK_REF))

    assert len(find_by_identity(doubled, identity)) == 2


@pytest.mark.parametrize("malformed", ["not-a-ref", "item:only:three", ""])
def test_a_reference_this_side_cannot_parse_does_not_blind_the_search(malformed: str) -> None:
    """One unparsable entry must not hide the object that is genuinely there."""
    identity = identity_of(_ref(generation=0))
    inventory = _inventory(malformed, _ref(generation=0))

    assert len(find_by_identity(inventory, identity)) == 1
