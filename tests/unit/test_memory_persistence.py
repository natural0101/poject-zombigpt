"""Memory on disk: one file per save, migrated forward or refused outright."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.memory import (
    CEILINGS,
    MAX_MEMORY_BYTES,
    MEMORY_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA,
    REASON_MIGRATED,
    MemoryConfig,
    MemorySchemaError,
    MemoryScopeError,
    MemoryStore,
    MemoryStoreError,
    SaveMemory,
    Square,
    TaskOutcome,
    document_version,
    migrate_document,
    persistence,
)
from pz_agent_core.observation.compact import save_scope
from pz_agent_core.protocol import ContainerKind
from tests.fixtures import DEFAULT_SAVE
from tests.fixtures.autonomy_worlds import NOW_MS, memory_with_containers, world_container_ref

OTHER_SAVE = "Rosewood, KY\\other-survivor"


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory")


def _populated(save_id: str = DEFAULT_SAVE) -> SaveMemory:
    memory = SaveMemory(save_id)
    memory.note_container(
        container_ref=world_container_ref(),
        kind=ContainerKind.WORLD,
        name="Counter",
        now_ms=NOW_MS,
        categories=["Food"],
        inspected=True,
    )
    memory.set_home(Square(x=1200, y=3400, z=0), NOW_MS)
    memory.reserve(full_type="Base.Whiskey", reason="trade goods", now_ms=NOW_MS)
    memory.record_task(key="loot", outcome=TaskOutcome.SUCCEEDED, now_ms=NOW_MS)
    return memory


def _v1_document(save_id: str = DEFAULT_SAVE) -> dict[str, Any]:
    """A document in the first on-disk layout, as an older build wrote it."""
    return {
        "schema_version": 1,
        "save_scope": save_scope(save_id),
        "containers": [
            {
                "tail": "world:1200:3400:0:1:0",
                "kind": "world",
                "label": "Counter",
                "square": {"x": 1200, "y": 3400, "z": 0},
                "last_seen_ms": NOW_MS,
            }
        ],
        "failed_paths": [],
        "reservations": ["Base.Whiskey"],
        "tasks": [],
        "home": {"square": {"x": 1200, "y": 3400, "z": 0}, "set_at_ms": NOW_MS},
    }


# --------------------------------------------------------------------------
# the file, and where it lives
# --------------------------------------------------------------------------


def test_the_filename_is_a_digest_not_the_save_id(store: MemoryStore) -> None:
    """A save id is a directory fragment; putting one in a path is a traversal."""
    path = store.path_for(OTHER_SAVE)

    assert path.name == f"memory.{save_scope(OTHER_SAVE)}.json"
    assert "Rosewood" not in path.name
    assert path.parent == store.root


def test_there_is_no_unscoped_memory_file(store: MemoryStore) -> None:
    with pytest.raises(ValueError, match="memory is always save-scoped"):
        store.path_for("")


def test_a_first_run_loads_an_empty_memory_rather_than_failing(store: MemoryStore) -> None:
    memory = store.load(DEFAULT_SAVE)

    assert memory.containers() == ()
    assert memory.reservations() == ()


def test_a_saved_memory_comes_back_whole(store: MemoryStore) -> None:
    written = store.save(_populated())
    restored = store.load(DEFAULT_SAVE)

    assert written > 0
    assert restored.home_point() == Square(x=1200, y=3400, z=0)
    assert restored.reserves_item("Base.Whiskey")
    assert [record.label for record in restored.containers()] == ["Counter"]
    assert [task.key for task in restored.task_history()] == ["loot"]


def test_a_different_save_does_not_see_the_previous_ones_containers(
    store: MemoryStore,
) -> None:
    store.save(_populated())

    other = store.load(OTHER_SAVE)

    assert other.containers() == ()
    assert not other.reserves_item("Base.Whiskey")
    assert other.save_id == OTHER_SAVE


def test_a_file_moved_under_another_saves_name_is_refused(store: MemoryStore) -> None:
    """The scope is inside the document too, so a renamed file cannot mis-load."""
    store.save(_populated())
    store.path_for(DEFAULT_SAVE).replace(store.path_for(OTHER_SAVE))

    with pytest.raises(MemoryScopeError, match="belongs to save"):
        store.load(OTHER_SAVE)


def test_saving_twice_leaves_no_scratch_file_behind(store: MemoryStore) -> None:
    memory = _populated()
    store.save(memory)
    store.save(memory)

    assert [path.name for path in sorted(store.root.iterdir())] == [
        store.path_for(DEFAULT_SAVE).name
    ]


def test_the_memory_can_be_forgotten(store: MemoryStore) -> None:
    store.save(_populated())

    assert store.forget(DEFAULT_SAVE)
    assert not store.forget(DEFAULT_SAVE)
    assert store.load(DEFAULT_SAVE).containers() == ()


# --------------------------------------------------------------------------
# a file that cannot be read is never silently replaced by an empty one
# --------------------------------------------------------------------------


def test_malformed_json_is_reported_rather_than_forgotten(store: MemoryStore) -> None:
    store.save(_populated())
    store.path_for(DEFAULT_SAVE).write_text("{not json", encoding="utf-8")

    with pytest.raises(MemoryStoreError, match="malformed JSON"):
        store.load(DEFAULT_SAVE)


def test_a_json_array_is_not_a_memory(store: MemoryStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.path_for(DEFAULT_SAVE).write_text("[]", encoding="utf-8")

    with pytest.raises(MemoryStoreError, match="expected a JSON object"):
        store.load(DEFAULT_SAVE)


def test_an_oversized_file_is_refused_before_it_is_parsed(store: MemoryStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.path_for(DEFAULT_SAVE).write_bytes(b" " * (MAX_MEMORY_BYTES + 1))

    with pytest.raises(MemoryStoreError, match=f"exceeds {MAX_MEMORY_BYTES}"):
        store.load(DEFAULT_SAVE)


def test_a_memory_at_every_ceiling_still_fits_under_the_byte_cap(tmp_path: Path) -> None:
    """The per-collection caps and the byte cap have to agree, or one is a lie.

    Every collection is filled to its ceiling with its longest legal strings —
    a version that filled only two of the six would leave the cheapest half of
    the document standing in for the whole of it.
    """
    config = MemoryConfig(**{name: ceiling for name, ceiling in CEILINGS.items()})
    store = MemoryStore(tmp_path, config=config)
    memory = SaveMemory(DEFAULT_SAVE, config=config)
    for index in range(CEILINGS["max_tasks"]):
        memory.record_task(
            key=f"task-{index}".ljust(64, "x"),
            outcome=TaskOutcome.FAILED,
            now_ms=NOW_MS + index,
            detail="d" * 120,
        )
    for index in range(CEILINGS["max_containers"]):
        memory.note_container(
            container_ref=world_container_ref(x=1200 + index),
            kind=ContainerKind.WORLD,
            name=f"Shelf {index}".ljust(64, "x"),
            now_ms=NOW_MS + index,
            categories=[
                f"category-{slot}".ljust(32, "y")
                for slot in range(CEILINGS["max_categories_per_container"])
            ],
            inspected=True,
        )
    for index in range(CEILINGS["max_failed_paths"]):
        memory.note_failed_path(
            origin=Square(0, 0, 0),
            target=Square(index, 10, 0),
            reason="r" * 120,
            now_ms=NOW_MS + index,
        )
    for index in range(CEILINGS["max_reservations"]):
        memory.reserve(
            full_type=f"Base.Item{index}".ljust(96, "z"), reason="z" * 120, now_ms=NOW_MS
        )
    for index in range(CEILINGS["max_preferences"]):
        memory.set_preference(key=f"pref{index}".ljust(48, "p"), value="v" * 64, now_ms=NOW_MS)
    for index in range(CEILINGS["max_safe_zones"]):
        memory.add_safe_zone(
            key=f"zone{index}".ljust(64, "q"),
            centre=Square(index, 0, 0),
            radius=5,
            label="L" * 64,
            now_ms=NOW_MS + index,
        )

    assert len(memory.reservations()) == CEILINGS["max_reservations"]
    assert len(memory.containers()) == CEILINGS["max_containers"]
    assert store.save(memory) <= MAX_MEMORY_BYTES


def test_a_document_over_the_byte_cap_is_refused_on_write(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(persistence, "MAX_MEMORY_BYTES", 16)

    with pytest.raises(MemoryStoreError, match="exceeds 16"):
        store.save(_populated())


def test_a_write_that_the_filesystem_refuses_is_reported(tmp_path: Path) -> None:
    blocked = tmp_path / "memory"
    blocked.write_text("not a directory", encoding="utf-8")
    store = MemoryStore(blocked)

    with pytest.raises(MemoryStoreError, match="cannot write memory"):
        store.save(_populated())


# --------------------------------------------------------------------------
# schema versions
# --------------------------------------------------------------------------


def test_a_document_with_no_version_is_refused() -> None:
    with pytest.raises(MemorySchemaError, match="declares no schema_version"):
        document_version({"save_scope": "abc"})


def test_a_non_integer_version_is_refused() -> None:
    with pytest.raises(MemorySchemaError, match="must be an integer"):
        document_version({"schema_version": "2"})


def test_a_document_from_a_newer_build_is_refused_not_reinterpreted() -> None:
    document = {"schema_version": MEMORY_SCHEMA_VERSION + 1, "save_scope": "abc"}

    with pytest.raises(MemorySchemaError, match="A newer pz-agent wrote it"):
        migrate_document(document)


def test_a_document_older_than_the_migration_chain_is_refused() -> None:
    document = {"schema_version": MIN_SUPPORTED_SCHEMA - 1, "save_scope": "abc"}

    with pytest.raises(MemorySchemaError, match="no longer upgrades"):
        migrate_document(document)


def test_the_current_version_passes_through_unchanged() -> None:
    document = _populated().to_document(schema_version=MEMORY_SCHEMA_VERSION)

    assert migrate_document(document) == document


def test_a_schema_one_document_is_migrated_forward() -> None:
    migrated = migrate_document(_v1_document())

    assert migrated["schema_version"] == MEMORY_SCHEMA_VERSION
    assert migrated["containers"][0]["last_inspected_ms"] == 0
    assert migrated["containers"][0]["categories"] == []
    assert migrated["safe_zones"] == []
    assert migrated["preferences"] == []


def test_a_migrated_reservation_says_its_reason_was_never_recorded() -> None:
    """Inventing one would put words in the user's mouth and show them back."""
    (reservation,) = migrate_document(_v1_document())["reservations"]

    assert reservation["full_type"] == "Base.Whiskey"
    assert reservation["reason"] == REASON_MIGRATED


def test_a_migrated_document_loads_into_a_memory(store: MemoryStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.path_for(DEFAULT_SAVE).write_text(json.dumps(_v1_document()), encoding="utf-8")

    memory = store.load(DEFAULT_SAVE)

    assert memory.reserves_item("Base.Whiskey")
    assert memory.home_point() == Square(x=1200, y=3400, z=0)
    assert [record.tail for record in memory.containers()] == ["world:1200:3400:0:1:0"]


def test_a_migrated_document_is_rewritten_at_the_current_version(store: MemoryStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.path_for(DEFAULT_SAVE).write_text(json.dumps(_v1_document()), encoding="utf-8")

    store.save(store.load(DEFAULT_SAVE))
    rewritten = json.loads(store.path_for(DEFAULT_SAVE).read_text(encoding="utf-8"))

    assert rewritten["schema_version"] == MEMORY_SCHEMA_VERSION


def test_a_file_from_the_future_is_refused_through_the_store_too(store: MemoryStore) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    document = _populated().to_document(schema_version=MEMORY_SCHEMA_VERSION)
    document["schema_version"] = MEMORY_SCHEMA_VERSION + 5
    store.path_for(DEFAULT_SAVE).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MemorySchemaError):
        store.load(DEFAULT_SAVE)


def test_a_broken_v1_collection_is_refused_rather_than_migrated_half_way() -> None:
    document = _v1_document()
    document["containers"] = {"not": "an array"}

    with pytest.raises(MemorySchemaError, match="expected an array"):
        migrate_document(document)


# --------------------------------------------------------------------------
# caps survive the round trip
# --------------------------------------------------------------------------


def test_the_container_cap_still_holds_after_a_reload(tmp_path: Path) -> None:
    config = MemoryConfig(max_containers=3)
    store = MemoryStore(tmp_path, config=config)
    store.save(memory_with_containers(DEFAULT_SAVE, 8, config=config))

    assert len(store.load(DEFAULT_SAVE).containers()) == 3
