"""What ``pz-agent remember`` accepts, what it refuses, and what the seam answers.

``tests/contract/test_sidecar_memory_wiring.py`` proves the store reaches the
assembled loop and that a reservation actually stops a meal. This file is about
the two halves of that seam taken one rule at a time: the validation a user's
argument passes through on its way into a stored document, and the states
:class:`~pz_agent_cli.memory.SidecarMemory` can be in — nothing loaded, loaded,
re-loaded because another process wrote the file, dropped because the save
changed, and the one that is not an absence at all: a memory file that exists and
will not read back.

Every command is driven through the real :func:`~pz_agent_cli.app.main`, against
a snapshot published the way the mod publishes one, because "which save is this
for" is a question only the mod can answer and a test that supplied the save id
itself would be testing a different command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from pz_agent_cli.memory import (
    MEMORY_DIR_NAME,
    MEMORY_RECORD_NAME,
    NOTE_FLUSH_INTERVAL_MS,
    RESERVED_BY_USER,
    MemoryRecord,
    MemoryRecordError,
    RememberRefused,
    SidecarMemory,
    build_sidecar_memory,
    memory_root,
    memory_store,
    publish_memory_record,
    read_memory_record,
    validate_full_type,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.snapshot import SnapshotWriter
from pz_agent_core.memory import (
    MAX_FULL_TYPE_LEN,
    MemoryStore,
    SaveMemory,
    Square,
    content_revision_of,
)
from pz_agent_core.policy.autonomy import NO_MEMORY
from pz_agent_core.protocol import (
    ContainerKind,
    InventoryView,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    item_ref,
    make_container,
    make_game,
    make_item,
    make_observation,
    make_player,
)
from tests.fixtures.cli_worlds import CliWorld, make_world

SAVE: Final = "1f3c9a2b7e40d115"
OTHER_SAVE: Final = "9911aabb00cc22dd"
BEANS: Final = "Base.TinnedBeans"

#: Where the character stands in the published snapshot.
POSITION: Final = (1200.0, 3400.0, 0)

NOW_MS: Final = 1_700_000_000_000


# ---------------------------------------------------------------------------
# a machine with a mod publishing snapshots
# ---------------------------------------------------------------------------


def publish_snapshot(
    world: CliWorld,
    *,
    save_id: str = SAVE,
    seq: int = 1,
    position: tuple[float, float, int] = POSITION,
) -> None:
    """Publish a full snapshot the way the mod does: slot first, pointer last."""
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    observation = make_observation(
        seq=seq,
        timestamp_ms=world.clock.now_ms,
        game=make_game(save_id=save_id),
        player=make_player(
            position=Position(x=position[0], y=position[1], z=position[2], direction="S")
        ),
    )
    SnapshotWriter(layout, clock=lambda: world.clock.now_ms).publish(observation.to_dict())


def attached_world(
    tmp_path: Path,
    *,
    save_id: str = SAVE,
    position: tuple[float, float, int] = POSITION,
) -> CliWorld:
    """A machine whose mod has published a snapshot, with the clock held still."""
    world = make_world(tmp_path)
    world.clock.freeze()
    publish_snapshot(world, save_id=save_id, position=position)
    return world


def stored(world: CliWorld, save_id: str = SAVE) -> SaveMemory:
    """Read this save's memory back off disk, through the real store."""
    return memory_store(resolve_workspace(world.ctx)).load(save_id)


def run(world: CliWorld, *argv: str) -> int:
    world.reset_streams()
    return world.run(*argv)


# ---------------------------------------------------------------------------
# what a user may type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "full_type",
    ["Base.TinnedBeans", "Base.Water", "Hydrocraft.HCBucket", "A.b", "Base.Tinned_Beans_2"],
)
def test_an_item_type_shaped_like_one_is_accepted(full_type: str) -> None:
    assert validate_full_type(full_type) == full_type


@pytest.mark.parametrize(
    "full_type",
    [
        "",
        "   ",
        "Base",
        "Base.",
        ".TinnedBeans",
        "Base.Tinned.Beans",
        "Base.Tinned Beans",
        "../../etc/passwd",
        "Base/TinnedBeans",
        "Base.Tinned\nBeans",
        "Base.Tinned\x00Beans",
        "Base.Ти́нned",
        '{"full_type": "Base.X"}',
    ],
)
def test_anything_that_is_not_an_item_type_is_refused(full_type: str) -> None:
    """This string becomes a key in a document this process writes."""
    with pytest.raises(RememberRefused):
        validate_full_type(full_type)


def test_an_item_type_longer_than_the_store_accepts_is_refused_before_it_is_matched() -> None:
    """Bounded first: the length check is what keeps a huge argument cheap."""
    with pytest.raises(RememberRefused, match=str(MAX_FULL_TYPE_LEN)):
        validate_full_type("Base." + "A" * MAX_FULL_TYPE_LEN)


def test_surrounding_whitespace_is_taken_off_rather_than_stored(tmp_path: Path) -> None:
    world = attached_world(tmp_path)

    assert run(world, "remember", "reserve", f"  {BEANS} ") == EXIT_OK, world.stderr

    assert [record.full_type for record in stored(world).reservations()] == [BEANS]


# ---------------------------------------------------------------------------
# with no session attached
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("remember", "home"),
        ("remember", "reserve", BEANS),
        ("remember", "release", BEANS),
        ("remember", "list"),
        ("remember", "forget"),
    ],
)
def test_every_command_refuses_with_no_session_and_writes_nothing(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """There is no save to scope a memory to, and guessing one is the failure."""
    world = make_world(tmp_path)

    assert run(world, *argv) == EXIT_FAILURE
    assert "no session is attached" in world.stderr
    assert not memory_root(resolve_workspace(world.ctx)).exists()


def test_a_snapshot_too_old_to_describe_the_open_save_is_not_a_session(tmp_path: Path) -> None:
    """A game that quit leaves its last snapshot on disk for ever."""
    world = attached_world(tmp_path)
    world.clock.advance(60_000)

    assert run(world, "remember", "reserve", BEANS) == EXIT_FAILURE
    assert "ms old" in world.stderr


def test_a_save_the_mod_could_not_name_is_not_one_to_remember_for(tmp_path: Path) -> None:
    """Every unreadable save reports the same id, so it names none in particular."""
    world = attached_world(tmp_path, save_id="unknown")

    assert run(world, "remember", "reserve", BEANS) == EXIT_FAILURE
    assert "reported it as unknown" in world.stderr


def test_the_refusal_is_machine_readable_too(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert run(world, "remember", "home", "--json") == EXIT_FAILURE

    payload = json.loads(world.stdout)
    assert payload["changed"] is False
    assert payload["attached"] is False


def test_remember_without_a_subcommand_names_the_ones_there_are(tmp_path: Path) -> None:
    world = attached_world(tmp_path)

    assert run(world, "remember") == EXIT_FAILURE
    assert "reserve" in world.stderr


# ---------------------------------------------------------------------------
# what the commands write
# ---------------------------------------------------------------------------


def test_remember_home_records_the_square_the_mod_reported(tmp_path: Path) -> None:
    """Not an argument: the point is where the character *is*."""
    world = attached_world(tmp_path, position=(1207.8, 3399.2, 0))

    assert run(world, "remember", "home") == EXIT_OK, world.stderr

    assert stored(world).home_point() == Square(x=1207, y=3399, z=0)
    assert "1207, 3399" in world.stdout


def test_a_reservation_carries_the_reason_it_exists(tmp_path: Path) -> None:
    world = attached_world(tmp_path)

    assert run(world, "remember", "reserve", BEANS) == EXIT_OK, world.stderr

    held = stored(world).reservations()
    assert [record.full_type for record in held] == [BEANS]
    assert held[0].reason == RESERVED_BY_USER


def test_releasing_something_that_was_never_reserved_changes_nothing(tmp_path: Path) -> None:
    world = attached_world(tmp_path)

    assert run(world, "remember", "release", BEANS) == EXIT_OK
    assert "was not reserved" in world.stdout
    assert not memory_store(resolve_workspace(world.ctx)).path_for(SAVE).is_file()


def test_list_reports_the_home_point_the_reservations_and_the_file(tmp_path: Path) -> None:
    world = attached_world(tmp_path)
    assert run(world, "remember", "reserve", BEANS) == EXIT_OK
    assert run(world, "remember", "home") == EXIT_OK

    assert run(world, "remember", "list", "--json") == EXIT_OK

    payload = json.loads(world.stdout)
    assert payload["home"] == {"x": 1200, "y": 3400, "z": 0}
    assert [record["full_type"] for record in payload["reservations"]] == [BEANS]
    assert MEMORY_DIR_NAME in payload["file"]


def test_forget_removes_this_saves_memory_and_says_when_there_was_none(tmp_path: Path) -> None:
    world = attached_world(tmp_path)
    assert run(world, "remember", "reserve", BEANS) == EXIT_OK

    assert run(world, "remember", "forget") == EXIT_OK
    assert not memory_store(resolve_workspace(world.ctx)).path_for(SAVE).is_file()

    assert run(world, "remember", "forget") == EXIT_OK
    assert "nothing remembered" in world.stdout


def test_one_saves_reservations_are_not_written_into_anothers_file(tmp_path: Path) -> None:
    world = attached_world(tmp_path)
    assert run(world, "remember", "reserve", BEANS) == EXIT_OK

    publish_snapshot(world, save_id=OTHER_SAVE, seq=2)
    assert run(world, "remember", "list", "--json") == EXIT_OK

    assert json.loads(world.stdout)["reservations"] == []
    assert [record.full_type for record in stored(world).reservations()] == [BEANS]


def test_reservations_are_bounded_by_the_stores_own_cap(tmp_path: Path) -> None:
    """The store refuses rather than dropping one of the user's own records."""
    world = attached_world(tmp_path)
    store = memory_store(resolve_workspace(world.ctx))
    memory = SaveMemory(SAVE)
    for index in range(memory.config.max_reservations):
        memory.reserve(full_type=f"Base.Item{index}", reason="filled by a test", now_ms=NOW_MS)
    store.save(memory)

    assert run(world, "remember", "reserve", BEANS) == EXIT_FAILURE

    assert "release one" in world.stderr
    assert "Nothing was changed" in world.stderr
    assert not store.load(SAVE).reserves_item(BEANS)


def test_a_memory_that_will_not_read_back_is_reported_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    """The reservations in it are the user's; a fresh file would silently drop them."""
    world = attached_world(tmp_path)
    store = memory_store(resolve_workspace(world.ctx))
    path = store.path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert run(world, "remember", "reserve", BEANS) == EXIT_FAILURE

    assert "could not be read" in world.stderr
    assert path.read_text(encoding="utf-8") == "{not json"


def test_an_unreadable_memory_can_still_be_forgotten(tmp_path: Path) -> None:
    """Otherwise the one file a user has to be able to delete is the one they cannot."""
    world = attached_world(tmp_path)
    path = memory_store(resolve_workspace(world.ctx)).path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert run(world, "remember", "forget") == EXIT_OK

    assert not path.is_file()


# ---------------------------------------------------------------------------
# the seam itself
# ---------------------------------------------------------------------------


def seam(tmp_path: Path) -> tuple[SidecarMemory, MemoryStore]:
    """A :class:`SidecarMemory` over a temporary store, as ``build_loop`` builds it."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    return build_sidecar_memory(workspace), memory_store(workspace)


def test_nothing_is_loaded_until_a_save_is_named(tmp_path: Path) -> None:
    memory, _ = seam(tmp_path)

    assert memory.loaded is None
    assert memory.in_force is NO_MEMORY
    assert memory.reserves_item(BEANS) is False
    assert memory.home_point() is None


@pytest.mark.parametrize("save_id", ["", "unknown"])
def test_a_tick_that_names_no_save_loads_nothing(tmp_path: Path, save_id: str) -> None:
    """An id every unreadable save reports is not an id to load a world by."""
    memory, _ = seam(tmp_path)

    memory.follow(save_id)

    assert memory.loaded is None
    assert memory.in_force is NO_MEMORY


def test_the_first_save_named_is_the_one_loaded(tmp_path: Path) -> None:
    memory, store = seam(tmp_path)
    saved = SaveMemory(SAVE)
    saved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    store.save(saved)

    memory.follow(SAVE)

    assert memory.reserves_item(BEANS) is True
    loaded = memory.loaded
    assert loaded is not None and loaded.save_id == SAVE


def test_a_save_that_changes_drops_what_the_previous_one_remembered(tmp_path: Path) -> None:
    memory, store = seam(tmp_path)
    saved = SaveMemory(SAVE)
    saved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    saved.set_home(Square(x=1, y=2, z=0), NOW_MS)
    store.save(saved)
    memory.follow(SAVE)

    memory.follow(OTHER_SAVE)

    assert memory.reserves_item(BEANS) is False
    assert memory.home_point() is None


def test_a_file_written_by_another_process_is_read_on_the_next_tick(tmp_path: Path) -> None:
    """``pz-agent remember`` is that other process, and it runs while the loop does."""
    memory, store = seam(tmp_path)
    memory.follow(SAVE)
    assert memory.reserves_item(BEANS) is False

    reserved = store.load(SAVE)
    reserved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    store.save(reserved)
    memory.follow(SAVE)

    assert memory.reserves_item(BEANS) is True


def test_a_memory_that_cannot_be_read_reserves_everything(tmp_path: Path) -> None:
    """Not an empty memory: the user's reservations are in there, unread.

    Answering "nothing is reserved" because the document would not parse is
    exactly how the agent spends the thing somebody set aside, so the
    conservative reading stands in until the file can be read again.
    """
    memory, store = seam(tmp_path)
    path = store.path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    memory.follow(SAVE)

    assert memory.loaded is None
    assert memory.reserves_item(BEANS) is True
    assert memory.reserves_item("Base.Anything") is True
    assert memory.home_point() is None
    assert "could not be read" in memory.detail


def test_a_memory_repaired_between_ticks_stops_reserving_everything(tmp_path: Path) -> None:
    memory, store = seam(tmp_path)
    path = store.path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    memory.follow(SAVE)

    store.save(SaveMemory(SAVE))
    memory.follow(SAVE)

    assert memory.loaded is not None
    assert memory.reserves_item(BEANS) is False


# ---------------------------------------------------------------------------
# the record status reads
# ---------------------------------------------------------------------------


def test_the_record_says_nothing_is_loaded_before_a_save_is_named(tmp_path: Path) -> None:
    memory, _ = seam(tmp_path)

    record = memory.record()

    assert record.loaded is False
    assert record.readable is True
    assert record.save_scope == ""
    assert MEMORY_DIR_NAME in record.root


def test_the_record_counts_what_the_loaded_memory_holds(tmp_path: Path) -> None:
    memory, store = seam(tmp_path)
    saved = SaveMemory(SAVE)
    saved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    saved.set_home(Square(x=1, y=2, z=0), NOW_MS)
    store.save(saved)

    memory.follow(SAVE)

    record = read_memory_record(memory.record_path)
    assert record is not None
    assert record.loaded is True
    assert record.reservations == 1
    assert record.home is True
    assert record.save_scope == saved.scope


def test_the_record_says_when_the_memory_could_not_be_read(tmp_path: Path) -> None:
    memory, store = seam(tmp_path)
    path = store.path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    memory.follow(SAVE)

    record = read_memory_record(memory.record_path)
    assert record is not None
    assert record.readable is False
    assert record.loaded is False


def test_a_record_that_cannot_be_written_is_noted_rather_than_raised(tmp_path: Path) -> None:
    """Losing the status file is not a reason to stop honouring a reservation."""
    blocked = tmp_path / "in-the-way"
    blocked.mkdir()

    written = publish_memory_record(
        blocked, MemoryRecord(wired=True, loaded=False, root="", detail="nothing loaded yet")
    )

    assert written.notes
    assert "could not be written" in written.notes[-1]


def test_a_record_round_trips(tmp_path: Path) -> None:
    record = MemoryRecord(
        wired=True,
        loaded=True,
        root="<ZOMBOID>/pz-agent/memory",
        detail="loaded",
        save_scope="abc123",
        reservations=2,
        home=True,
    )
    path = tmp_path / MEMORY_RECORD_NAME

    publish_memory_record(path, record)

    assert read_memory_record(path) == record


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"wired": True},
        {"wired": "yes", "loaded": False, "detail": "d"},
        {"wired": True, "loaded": True, "detail": "d", "reservations": -1},
        {"wired": True, "loaded": True, "detail": "d", "notes": "one note"},
        {"wired": True, "loaded": True, "detail": "   "},
    ],
)
def test_a_malformed_record_is_no_record_rather_than_half_of_one(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / MEMORY_RECORD_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_memory_record(path) is None
    with pytest.raises(MemoryRecordError):
        MemoryRecord.from_dict(payload)


def test_no_record_at_all_is_a_different_answer_from_a_bad_one(tmp_path: Path) -> None:
    assert read_memory_record(tmp_path / MEMORY_RECORD_NAME) is None


# ---------------------------------------------------------------------------
# the feeding seam: sightings and inspections
# ---------------------------------------------------------------------------

WORLD_TAIL: Final = "world:1210:3405:0:1:0"
WORLD_REF: Final = f"container:{DEFAULT_SESSION}:{WORLD_TAIL}"
SECOND_TAIL: Final = "world:1215:3406:0:1:0"
SECOND_REF: Final = f"container:{DEFAULT_SESSION}:{SECOND_TAIL}"


def nearby_container(ref: str = WORLD_REF) -> NearbyObject:
    return NearbyObject(ref=ref, kind="container", distance=1.5)


def world_observation(
    *,
    save_id: str = SAVE,
    objects: list[NearbyObject] | None = None,
    inventory: InventoryView | None = None,
) -> Observation:
    return make_observation(
        game=make_game(save_id=save_id),
        nearby=NearbyView(objects=[nearby_container()] if objects is None else objects),
        inventory=InventoryView() if inventory is None else inventory,
    )


def counter_inventory() -> InventoryView:
    """A described world counter holding two tins and a hammer."""
    return InventoryView(
        containers=[make_container(WORLD_REF, ContainerKind.WORLD, name="Counter")],
        items=[
            make_item(item_ref("beans-1", WORLD_TAIL), WORLD_REF),
            make_item(item_ref("beans-2", WORLD_TAIL), WORLD_REF),
            make_item(
                item_ref("hammer-1", WORLD_TAIL),
                WORLD_REF,
                full_type="Base.Hammer",
                display_name="Hammer",
                category="Tool",
            ),
        ],
    )


def test_a_world_container_in_nearby_becomes_a_sighting(tmp_path: Path) -> None:
    """The seam that finally feeds note_container: seen, where, and nothing more."""
    memory, store = seam(tmp_path)

    recorded = memory.note_observation(world_observation(), now_ms=NOW_MS)

    assert recorded == 1
    loaded = memory.loaded
    assert loaded is not None
    (record,) = loaded.containers()
    assert record.tail == WORLD_TAIL
    assert record.square is not None and (record.square.x, record.square.y) == (1210, 3405)
    assert record.last_inspected_ms == 0
    assert record.item_count == -1
    assert store.path_for(SAVE).is_file(), "the first sighting was never flushed"


def test_a_sighting_of_a_described_container_records_its_kind_and_name(tmp_path: Path) -> None:
    memory, _ = seam(tmp_path)
    observation = world_observation(inventory=counter_inventory())

    memory.note_observation(observation, now_ms=NOW_MS)

    loaded = memory.loaded
    assert loaded is not None
    (record,) = loaded.containers()
    assert record.kind is ContainerKind.WORLD
    assert record.label == "Counter"


def test_nothing_on_the_player_is_ever_stored(tmp_path: Path) -> None:
    """Worn and carried references embed runtime ids a reload invalidates."""
    memory, store = seam(tmp_path)
    observation = world_observation(
        objects=[
            nearby_container(f"container:{DEFAULT_SESSION}:player-main"),
            nearby_container(backpack_container_ref()),
            NearbyObject(ref=f"door:{DEFAULT_SESSION}:10:20:0:1", kind="door", distance=1.0),
        ]
    )

    recorded = memory.note_observation(observation, now_ms=NOW_MS)

    assert recorded == 0
    loaded = memory.loaded
    assert loaded is not None and loaded.containers() == ()
    assert not store.path_for(SAVE).is_file(), "a memory with nothing to note wrote a file"


def test_sightings_are_flushed_on_a_cadence_not_per_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned bound: at most one write per NOTE_FLUSH_INTERVAL_MS."""
    memory, _ = seam(tmp_path)
    writes: list[int] = []
    real_save = memory.store.save

    def counting_save(saved: SaveMemory) -> int:
        written = real_save(saved)
        writes.append(written)
        return written

    monkeypatch.setattr(memory.store, "save", counting_save)

    memory.note_observation(world_observation(), now_ms=NOW_MS)
    memory.note_observation(world_observation(), now_ms=NOW_MS + 1_000)
    memory.note_observation(world_observation(), now_ms=NOW_MS + 2_000)
    assert len(writes) == 1, "a write per observation is the disk workload this seam avoids"

    memory.note_observation(world_observation(), now_ms=NOW_MS + NOTE_FLUSH_INTERVAL_MS)
    assert len(writes) == 2

    memory.note_observation(world_observation(), now_ms=NOW_MS + NOTE_FLUSH_INTERVAL_MS + 1_000)
    assert len(writes) == 2


def test_an_inspection_records_the_revision_the_count_and_the_categories(
    tmp_path: Path,
) -> None:
    memory, _ = seam(tmp_path)
    observation = world_observation(inventory=counter_inventory())
    memory.note_observation(observation, now_ms=NOW_MS)

    assert memory.note_inspection(WORLD_REF, observation, now_ms=NOW_MS + 500) is True

    loaded = memory.loaded
    assert loaded is not None
    record = loaded.container(WORLD_REF)
    assert record is not None
    assert record.item_count == 3
    assert record.last_inspected_ms == NOW_MS + 500
    assert record.categories == ("Food", "Tool")
    expected = content_revision_of([("Base.TinnedBeans", 2), ("Base.Hammer", 1)])
    assert record.content_revision == expected
    assert loaded.container_unchanged(WORLD_TAIL, expected) is True


def test_an_inspection_of_an_on_person_container_is_refused(tmp_path: Path) -> None:
    memory, _ = seam(tmp_path)
    observation = world_observation(inventory=counter_inventory())
    memory.note_observation(observation, now_ms=NOW_MS)

    refused = memory.note_inspection(
        f"container:{DEFAULT_SESSION}:player-main", observation, now_ms=NOW_MS
    )

    assert refused is False
    loaded = memory.loaded
    assert loaded is not None
    assert [record.tail for record in loaded.containers()] == [WORLD_TAIL]


def test_an_inspection_the_observation_does_not_describe_is_not_stored(tmp_path: Path) -> None:
    """ "The crate was not described" is the opposite fact from "the crate is empty"."""
    memory, _ = seam(tmp_path)
    observation = world_observation()
    memory.note_observation(observation, now_ms=NOW_MS)

    assert memory.note_inspection(SECOND_REF, observation, now_ms=NOW_MS) is False

    loaded = memory.loaded
    assert loaded is not None
    assert loaded.container(SECOND_REF) is None


def test_an_unreadable_memory_is_fed_nothing(tmp_path: Path) -> None:
    """A file that will not parse must not be overwritten by a passing shelf."""
    memory, store = seam(tmp_path)
    path = store.path_for(SAVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    recorded = memory.note_observation(world_observation(), now_ms=NOW_MS)

    assert recorded == 0
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_reservation_written_mid_run_survives_the_seams_flush(tmp_path: Path) -> None:
    """The user's words outrank the agent's sightings, whoever wrote last."""
    memory, store = seam(tmp_path)
    memory.note_observation(world_observation(), now_ms=NOW_MS)

    reserved = store.load(SAVE)
    reserved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    store.save(reserved)

    memory.note_observation(
        world_observation(objects=[nearby_container(SECOND_REF)]),
        now_ms=NOW_MS + NOTE_FLUSH_INTERVAL_MS,
    )

    assert memory.reserves_item(BEANS) is True
    on_disk = store.load(SAVE)
    assert on_disk.reserves_item(BEANS) is True
    assert {record.tail for record in on_disk.containers()} == {WORLD_TAIL, SECOND_TAIL}


def test_a_write_landing_between_load_and_flush_is_not_clobbered(tmp_path: Path) -> None:
    """The narrow race: the file changes after follow() looked and before the flush.

    note_inspection does not re-follow, so an external write in that window is
    exactly what the flush's own stamp check exists to catch. The inspection is
    discarded as the smaller loss — re-derivable by looking again — and the
    reservation the user just made is not.
    """
    memory, store = seam(tmp_path)
    observation = world_observation(inventory=counter_inventory())
    memory.note_observation(observation, now_ms=NOW_MS)

    reserved = store.load(SAVE)
    reserved.reserve(full_type=BEANS, reason=RESERVED_BY_USER, now_ms=NOW_MS)
    store.save(reserved)

    memory.note_inspection(WORLD_REF, observation, now_ms=NOW_MS + NOTE_FLUSH_INTERVAL_MS)

    on_disk = store.load(SAVE)
    assert on_disk.reserves_item(BEANS) is True, "the seam overwrote the user's reservation"
    assert memory.reserves_item(BEANS) is True
    (record,) = on_disk.containers()
    assert record.content_revision == "", "the discarded inspection leaked to disk anyway"
