"""Reference parsing.

The interesting cases are all about the fact that a container reference
contains colons, so the naive `split(":")` that a reader expects would corrupt
world references — and a corrupted reference resolves to *some other object*,
which is exactly the failure mode refs exist to prevent.
"""

from __future__ import annotations

import pytest

from pz_agent_core.protocol.refs import (
    ContainerRef,
    ItemRef,
    RefError,
    RefKind,
    SquareRef,
    ZombieRef,
    belongs_to_session,
    ref_kind,
)

SESSION = "5e5510aa-0000-4000-8000-000000000001"


class TestItemRef:
    def test_roundtrip_through_string(self) -> None:
        ref = ItemRef(
            session_id=SESSION, container_tail="player-main", runtime_id="4210", generation=0
        )
        assert ItemRef.parse(str(ref)) == ref

    def test_world_container_tail_survives_its_own_colons(self) -> None:
        raw = f"item:{SESSION}:world:1200:3400:0:2:1:4210:3"
        ref = ItemRef.parse(raw)

        assert ref.container_tail == "world:1200:3400:0:2:1"
        assert ref.runtime_id == "4210"
        assert ref.generation == 3
        assert ref.container_ref == f"container:{SESSION}:world:1200:3400:0:2:1"
        assert str(ref) == raw

    def test_container_ref_is_reconstructed_parseably(self) -> None:
        ref = ItemRef.parse(f"item:{SESSION}:worn:Back:99001:4210:0")
        container = ContainerRef.parse(ref.container_ref)

        assert container.tail == "worn:Back:99001"
        assert container.is_on_person

    @pytest.mark.parametrize(
        "raw",
        [
            "container:sess:player-main",  # wrong kind
            f"item:{SESSION}:player-main:4210",  # no generation
            f"item:{SESSION}:4210:0",  # no container tail
            f"item:{SESSION}:player-main:4210:notanumber",
            "item::player-main:4210:0",  # empty session
            f"item:{SESSION}:player-main:has space:0",
        ],
    )
    def test_malformed_refs_are_rejected(self, raw: str) -> None:
        with pytest.raises(RefError):
            ItemRef.parse(raw)


class TestContainerRef:
    def test_constructors_produce_parseable_refs(self) -> None:
        for ref in (
            ContainerRef.player_main(SESSION),
            ContainerRef.worn(SESSION, "Back", "99001"),
            ContainerRef.world(SESSION, x=1200, y=3400, z=0, object_index=2, container_index=1),
        ):
            assert ContainerRef.parse(str(ref)) == ref

    def test_classification(self) -> None:
        assert ContainerRef.player_main(SESSION).is_player_main
        assert ContainerRef.player_main(SESSION).is_on_person
        assert ContainerRef.worn(SESSION, "Back", "99001").is_on_person
        assert not ContainerRef.worn(SESSION, "Back", "99001").is_player_main

        world = ContainerRef.world(SESSION, x=1200, y=3400, z=0, object_index=2, container_index=1)
        assert world.is_world
        assert not world.is_on_person

    def test_tail_is_required(self) -> None:
        with pytest.raises(RefError):
            ContainerRef.parse(f"container:{SESSION}")


class TestSquareRef:
    def test_roundtrip(self) -> None:
        ref = SquareRef(session_id=SESSION, x=1200, y=3400, z=1)
        assert SquareRef.parse(str(ref)) == ref

    def test_chebyshev_distance_ignores_floor(self) -> None:
        a = SquareRef(SESSION, 1200, 3400, 0)
        b = SquareRef(SESSION, 1203, 3401, 2)
        assert a.chebyshev_distance(b) == 3

    def test_non_integer_coordinates_rejected(self) -> None:
        with pytest.raises(RefError):
            SquareRef.parse(f"square:{SESSION}:1200.5:3400:0")


class TestZombieRef:
    def test_roundtrip(self) -> None:
        ref = ZombieRef(session_id=SESSION, runtime_id="z-771", generation=2)
        assert ZombieRef.parse(str(ref)) == ref

    def test_generation_must_be_numeric(self) -> None:
        with pytest.raises(RefError):
            ZombieRef.parse(f"zombie:{SESSION}:z-771:latest")


class TestTrailingControlCharacters:
    """'$' matches before a trailing newline; the Lua parser's does not.

    ``pz-mod/42/media/lua/shared/PZAgent/Refs.lua`` anchors the same alphabets
    with Lua patterns, where ``$`` is a true end-of-string anchor, and its
    header states the invariant these two implementations exist to keep: the
    strings one builds and parses are byte-identical to the other's. A segment
    that Python accepts and the mod refuses is that invariant broken — the
    sidecar validates a plan step, submits it, and the mod answers
    ``INVALID_REF`` for a reference this side called well-formed.
    """

    def test_a_runtime_id_may_not_end_in_a_newline(self) -> None:
        with pytest.raises(RefError):
            ItemRef.parse(f"item:{SESSION}:player-main:4210\n:0")

    def test_a_container_tail_segment_may_not_end_in_a_newline(self) -> None:
        with pytest.raises(RefError):
            ContainerRef.parse(f"container:{SESSION}:worn:Back\n:99001")

    def test_a_session_segment_may_not_end_in_a_newline(self) -> None:
        with pytest.raises(RefError):
            ContainerRef.parse(f"container:{SESSION}\n:player-main")

    def test_a_zombie_runtime_id_may_not_end_in_a_newline(self) -> None:
        with pytest.raises(RefError):
            ZombieRef.parse(f"zombie:{SESSION}:z-771\n:2")


class TestSessionScoping:
    def test_ref_from_another_session_is_not_ours(self) -> None:
        other = "5e5510aa-0000-4000-8000-000000000002"
        raw = str(ItemRef(SESSION, "player-main", "4210", 0))

        assert belongs_to_session(raw, SESSION)
        assert not belongs_to_session(raw, other)

    def test_garbage_never_belongs_to_a_session(self) -> None:
        assert not belongs_to_session("not-a-ref", SESSION)
        assert not belongs_to_session("", SESSION)

    def test_ref_kind_classifies_without_full_parse(self) -> None:
        assert ref_kind(f"item:{SESSION}:player-main:1:0") is RefKind.ITEM
        assert ref_kind(f"square:{SESSION}:1:2:0") is RefKind.SQUARE

        with pytest.raises(RefError):
            ref_kind("player:whatever")
