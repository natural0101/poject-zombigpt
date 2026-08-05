"""Tier-1 diffs: the round trip is the whole contract."""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.observation.diff import (
    DiffError,
    ObservationDiff,
    apply_diff,
    diff_observations,
)
from pz_agent_core.protocol import (
    ContainerKind,
    Hands,
    InventoryView,
    NearbyView,
    Observation,
    Position,
)
from tests.fixtures import (
    backpack_container_ref,
    item_ref,
    main_container_ref,
    make_container,
    make_game,
    make_item,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.safety_builders import make_nearby, make_zombie

MAIN = main_container_ref()
BACKPACK = backpack_container_ref()


def _inventory(*item_ids: str) -> InventoryView:
    return InventoryView(
        containers=[make_container(MAIN, ContainerKind.PLAYER_MAIN)],
        items=[make_item(item_ref(i, "player-main"), MAIN) for i in item_ids],
    )


def _pairs() -> list[tuple[str, Observation, Observation]]:
    """Baseline/successor pairs covering every shape of change."""
    base = make_observation(inventory=_inventory("1", "2"), nearby=make_nearby())
    return [
        ("nothing changed", base, make_observation(inventory=_inventory("1", "2"))),
        (
            "scalar changed",
            base,
            make_observation(
                seq=2, inventory=_inventory("1", "2"), player=make_player(stats={"hunger": 0.8})
            ),
        ),
        (
            "list addition",
            base,
            make_observation(seq=2, inventory=_inventory("1", "2", "3")),
        ),
        (
            "list removal",
            base,
            make_observation(seq=2, inventory=_inventory("2")),
        ),
        (
            "list reordering only",
            base,
            make_observation(seq=2, inventory=_inventory("2", "1")),
        ),
        (
            "entry field changed",
            base,
            make_observation(
                seq=2,
                inventory=InventoryView(
                    containers=[make_container(MAIN, ContainerKind.PLAYER_MAIN)],
                    items=[
                        make_item(item_ref("1", "player-main"), MAIN, weight=9.5),
                        make_item(item_ref("2", "player-main"), MAIN),
                    ],
                ),
            ),
        ),
        (
            "optional field goes to None",
            make_observation(
                inventory=_inventory("1"), game=make_game(world_time="1993-07-09T10:00:00")
            ),
            make_observation(seq=2, inventory=_inventory("1"), game=make_game(world_time=None)),
        ),
        (
            "optional field appears",
            make_observation(inventory=_inventory("1"), player=make_player(hands=Hands())),
            make_observation(
                seq=2,
                inventory=_inventory("1"),
                player=make_player(hands=Hands(primary=item_ref("1", "player-main"))),
            ),
        ),
        (
            "whole sub-document goes to None",
            make_observation(inventory=_inventory("1"), nearby=make_nearby(make_zombie("z1", 4.0))),
            make_observation(seq=2, inventory=_inventory("1"), nearby=None),
        ),
        (
            "whole sub-document appears",
            make_observation(inventory=None),
            make_observation(seq=2, inventory=_inventory("1")),
        ),
        (
            "zombies added and removed at once",
            make_observation(nearby=make_nearby(make_zombie("z1", 4.0), make_zombie("z2", 9.0))),
            make_observation(
                seq=2,
                nearby=make_nearby(make_zombie("z2", 3.0, chasing=True), make_zombie("z3", 12.0)),
            ),
        ),
        (
            "position and safety changed together",
            base,
            make_observation(
                seq=2,
                inventory=_inventory("1", "2"),
                player=make_player(position=Position(x=1204.0, y=3398.0, z=1, direction="N")),
                safety=make_safety(armed=False, manual_takeover=True),
            ),
        ),
    ]


@pytest.mark.parametrize(("label", "previous", "current"), _pairs())
def test_applying_a_diff_reproduces_the_next_snapshot_exactly(
    label: str, previous: Observation, current: Observation
) -> None:
    rebuilt = apply_diff(previous, diff_observations(previous, current))
    assert rebuilt == current, label


@pytest.mark.parametrize(("label", "previous", "current"), _pairs())
def test_a_diff_survives_serialisation(
    label: str, previous: Observation, current: Observation
) -> None:
    wire = diff_observations(previous, current).to_dict()
    assert apply_diff(previous, ObservationDiff.from_dict(wire)) == current, label


def test_an_unchanged_snapshot_produces_an_empty_diff() -> None:
    observation = make_observation(inventory=_inventory("1"))
    assert diff_observations(observation, observation).is_empty


def test_a_diff_carries_only_what_changed() -> None:
    previous = make_observation(inventory=_inventory("1", "2"))
    current = make_observation(
        seq=2, inventory=_inventory("1", "2"), safety=make_safety(armed=False)
    )
    delta = diff_observations(previous, current).delta

    assert delta.assigned == {"seq": 2}
    assert set(delta.nested) == {"safety"}
    assert delta.nested["safety"].assigned == {"armed": False}
    # The inventory did not move, so it must not appear at all.
    assert "inventory" not in delta.nested
    assert "inventory" not in delta.lists


def test_a_reordered_list_travels_as_refs_not_as_entries() -> None:
    previous = make_observation(inventory=_inventory("1", "2"))
    current = make_observation(seq=2, inventory=_inventory("2", "1"))
    list_delta = diff_observations(previous, current).delta.nested["inventory"].lists["items"]

    assert list_delta.added == {}
    assert list_delta.removed == ()
    assert list_delta.changed == {}
    assert list_delta.order == (item_ref("2", "player-main"), item_ref("1", "player-main"))


def test_a_stat_that_changes_type_is_not_mistaken_for_unchanged() -> None:
    # True == 1 in Python; a diff that compares by equality alone loses this.
    previous = make_observation(player=make_player(stats={"cooked": True}))
    current = make_observation(seq=2, player=make_player(stats={"cooked": 1}))
    rebuilt = apply_diff(previous, diff_observations(previous, current))

    assert rebuilt.player.stats["cooked"] == 1
    assert not isinstance(rebuilt.player.stats["cooked"], bool)


def test_an_unkeyed_array_is_replaced_wholesale() -> None:
    previous = make_observation(
        inventory=InventoryView(
            items=[make_item(item_ref("1", "player-main"), MAIN, tags=["a", "b"])]
        )
    )
    current = make_observation(
        seq=2,
        inventory=InventoryView(items=[make_item(item_ref("1", "player-main"), MAIN, tags=["b"])]),
    )
    assert apply_diff(previous, diff_observations(previous, current)) == current


def test_a_diff_between_sessions_is_refused() -> None:
    previous = make_observation()
    current = make_observation(session_id=str(uuid.UUID(int=0xBEEF)), seq=2)
    with pytest.raises(DiffError, match="different sessions"):
        diff_observations(previous, current)


def test_a_backwards_diff_is_refused() -> None:
    previous = make_observation(seq=7)
    current = make_observation(seq=3)
    with pytest.raises(DiffError, match="precedes baseline"):
        diff_observations(previous, current)


def test_a_diff_will_not_apply_to_the_wrong_baseline() -> None:
    previous = make_observation(seq=1)
    current = make_observation(seq=2)
    diff = diff_observations(previous, current)

    with pytest.raises(DiffError, match="applies to seq 1"):
        apply_diff(make_observation(seq=5), diff)


def test_a_diff_will_not_apply_to_another_session() -> None:
    previous = make_observation(seq=1)
    diff = diff_observations(previous, make_observation(seq=2))
    other = make_observation(session_id=str(uuid.UUID(int=0xBEEF)), seq=1)

    with pytest.raises(DiffError, match="different session"):
        apply_diff(other, diff)


def test_a_diff_naming_an_absent_entry_is_refused_rather_than_guessed() -> None:
    previous = make_observation(inventory=_inventory("1", "2"))
    current = make_observation(
        seq=2,
        inventory=InventoryView(
            containers=[make_container(MAIN, ContainerKind.PLAYER_MAIN)],
            items=[
                make_item(item_ref("1", "player-main"), MAIN, weight=2.0),
                make_item(item_ref("2", "player-main"), MAIN),
            ],
        ),
    )
    diff = diff_observations(previous, current)
    stripped = make_observation(inventory=_inventory("2"))

    with pytest.raises(DiffError, match="absent from the baseline array"):
        apply_diff(stripped, diff)


def test_a_malformed_serialised_diff_is_rejected() -> None:
    with pytest.raises(DiffError, match="must be an integer"):
        ObservationDiff.from_dict(
            {"session_id": "s", "from_seq": "1", "to_seq": 2, "timestamp_ms": 0, "delta": {}}
        )
    with pytest.raises(DiffError, match="must be an object"):
        ObservationDiff.from_dict(
            {"session_id": "s", "from_seq": 1, "to_seq": 2, "timestamp_ms": 0, "delta": []}
        )


def test_diffing_ignores_container_order_but_reproduces_it() -> None:
    previous = make_observation(
        inventory=InventoryView(
            containers=[
                make_container(MAIN, ContainerKind.PLAYER_MAIN),
                make_container(BACKPACK, ContainerKind.WORN),
            ]
        )
    )
    current = make_observation(
        seq=2,
        inventory=InventoryView(
            containers=[
                make_container(BACKPACK, ContainerKind.WORN, accessible=False),
                make_container(MAIN, ContainerKind.PLAYER_MAIN),
            ]
        ),
    )
    rebuilt = apply_diff(previous, diff_observations(previous, current))
    assert rebuilt == current
    assert rebuilt.inventory is not None
    assert rebuilt.inventory.containers[0].ref == BACKPACK


def test_an_empty_nearby_view_still_round_trips() -> None:
    previous = make_observation(nearby=NearbyView())
    current = make_observation(seq=2, nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)))
    assert apply_diff(previous, diff_observations(previous, current)) == current
