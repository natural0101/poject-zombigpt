"""The content revision: a change detector for one container, and its bounds.

``content_revision_of`` exists so the loot planner can ask "has this shelf
changed since I searched it?" without memory holding an inventory. The
properties tested here are exactly the ones that question needs: the same
observed contents always produce the same digest however they were enumerated,
different contents (almost) never do, and the empty string stays reserved for
"never enumerated" — which is a different fact from "enumerated and empty".
"""

from __future__ import annotations

import pytest

from pz_agent_core.memory import (
    CONTENT_REVISION_HEX_LEN,
    MAX_CONTENT_REVISION_LEN,
    MemoryValueError,
    content_revision_of,
)
from pz_agent_core.memory.model import KnownContainer
from pz_agent_core.protocol import ContainerKind
from tests.fixtures.autonomy_worlds import NOW_MS

TAIL = "world:1200:3400:0:1:0"

SHELF = [("Base.TinnedBeans", 2), ("Base.Hammer", 1)]


def known(
    *,
    last_inspected_ms: int = 0,
    content_revision: str = "",
    item_count: int = -1,
) -> KnownContainer:
    return KnownContainer(
        tail=TAIL,
        kind=ContainerKind.WORLD,
        label="Counter",
        square=None,
        last_seen_ms=NOW_MS,
        last_inspected_ms=last_inspected_ms,
        content_revision=content_revision,
        item_count=item_count,
    )


# ---------------------------------------------------------------------------
# the digest itself
# ---------------------------------------------------------------------------


def test_the_revision_is_deterministic_for_the_same_contents() -> None:
    assert content_revision_of(SHELF) == content_revision_of(list(SHELF))


def test_enumeration_order_does_not_change_the_revision() -> None:
    """Two looks at the same shelf in a different order are the same fact."""
    assert content_revision_of(SHELF) == content_revision_of(list(reversed(SHELF)))


def test_how_the_counts_were_grouped_does_not_change_the_revision() -> None:
    """One pair of tins and two single tins are the same shelf."""
    grouped = content_revision_of([("Base.TinnedBeans", 2)])
    singly = content_revision_of([("Base.TinnedBeans", 1), ("Base.TinnedBeans", 1)])

    assert grouped == singly


def test_a_changed_count_changes_the_revision() -> None:
    """The property the loot planner spends: a looted shelf stops matching."""
    assert content_revision_of(SHELF) != content_revision_of(
        [("Base.TinnedBeans", 1), ("Base.Hammer", 1)]
    )


def test_a_changed_type_changes_the_revision() -> None:
    assert content_revision_of([("Base.TinnedBeans", 1)]) != content_revision_of(
        [("Base.Hammer", 1)]
    )


def test_a_type_cannot_run_into_its_count_and_alias_another_shelf() -> None:
    """The separator earns its place: ``A2`` x1 is not ``A`` x21."""
    assert content_revision_of([("Base.A2", 1)]) != content_revision_of([("Base.A", 21)])


def test_the_revision_is_a_short_fixed_width_hex_string() -> None:
    revision = content_revision_of(SHELF)

    assert len(revision) == CONTENT_REVISION_HEX_LEN
    assert set(revision) <= set("0123456789abcdef")
    assert len(revision) <= MAX_CONTENT_REVISION_LEN


def test_an_empty_enumeration_has_a_real_revision() -> None:
    """ "Opened and found empty" must not read as "never enumerated"."""
    revision = content_revision_of([])

    assert revision != ""
    assert len(revision) == CONTENT_REVISION_HEX_LEN


# ---------------------------------------------------------------------------
# the fields on the record
# ---------------------------------------------------------------------------


def test_the_defaults_mean_never_enumerated() -> None:
    record = known()

    assert record.content_revision == ""
    assert record.item_count == -1


def test_a_revision_over_the_bound_is_refused() -> None:
    with pytest.raises(MemoryValueError, match="content revision"):
        known(content_revision="f" * (MAX_CONTENT_REVISION_LEN + 1))


def test_an_item_count_below_the_never_enumerated_marker_is_refused() -> None:
    with pytest.raises(MemoryValueError, match="item_count"):
        known(item_count=-2)


def test_the_new_fields_round_trip_through_the_document_shape() -> None:
    record = known(
        last_inspected_ms=NOW_MS,
        content_revision=content_revision_of(SHELF),
        item_count=3,
    )

    restored = KnownContainer.from_dict(record.to_dict(), category_limit=12)

    assert restored.content_revision == record.content_revision
    assert restored.item_count == 3


def test_the_defaults_are_omitted_from_the_document_not_spelled_out() -> None:
    """A never-enumerated container's document says nothing about contents."""
    document = known().to_dict()

    assert "content_revision" not in document
    assert "item_count" not in document
    assert KnownContainer.from_dict(document, category_limit=12).item_count == -1
