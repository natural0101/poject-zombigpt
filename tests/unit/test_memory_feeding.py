"""The loop half of the container-memory seam: who feeds the store, and when.

``SaveMemory.note_container`` existed for a long time with no production
caller — the store could answer "which containers do I know?" and nothing had
ever told it one. The seam that closes that gap has two halves and this file
tests the loop's: :meth:`SidecarLoop.tick` offers each tick's observation to
the memory behind its planner (a sighting per visible world container), and a
terminal result whose evidence proves contents were observed is offered as an
inspection. What the memory *does* with those calls — the merge rules, the
digest, the flush cadence — is ``tests/unit/test_cli_memory.py``'s half.

The loop reaches the memory structurally (:class:`ContainerMemory`), because
the planner classes import the runtime module and the dependency cannot point
back. The fakes here have exactly the shape the shipped assembly has: a
wrapper exposing ``inner`` around a planner exposing ``memory``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.memory import NOTE_FLUSH_INTERVAL_MS, SidecarMemory
from pz_agent_cli.runtime import ContainerMemory
from pz_agent_core.actions.adapter import Evidence
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.memory import MemoryStore
from pz_agent_core.policy.autonomy import NO_MEMORY
from pz_agent_core.protocol import (
    ActionResult,
    ActionStatus,
    NearbyObject,
    NearbyView,
    Observation,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SAVE, make_game, make_observation
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.sidecar_worlds import SidecarWorld, attached_world, make_sidecar_world

WORLD_TAIL: Final = "world:1210:3405:0:1:0"


# ---------------------------------------------------------------------------
# the fakes, in the shipped assembly's shape
# ---------------------------------------------------------------------------


class MemoryHolder:
    """Stands for AutonomyPlanner: a planner whose ``memory`` is the seam's."""

    def __init__(self, memory: object) -> None:
        self.memory = memory

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None


class Wrapper:
    """Stands for NavigatingPlanner: delegates, and exposes ``inner``."""

    def __init__(self, inner: MemoryHolder) -> None:
        self.inner = inner

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None


class RecordingMemory:
    """A :class:`ContainerMemory` that only remembers being called."""

    def __init__(self) -> None:
        self.observations: list[tuple[int, int]] = []
        self.inspections: list[str] = []
        self.flushes = 0

    def note_observation(self, observation: Observation, *, now_ms: int) -> int:
        self.observations.append((observation.seq, now_ms))
        return 0

    def note_inspection(self, container_ref: str, observation: Observation, *, now_ms: int) -> bool:
        self.inspections.append(container_ref)
        return True

    def flush_notes(self, *, now_ms: int) -> bool:
        self.flushes += 1
        return False


def sidecar_memory(tmp_path: Path) -> SidecarMemory:
    return SidecarMemory(
        store=MemoryStore(tmp_path / "memory-store"),
        record_path=tmp_path / "sidecar.memory.json",
    )


def world_container_object(world: SidecarWorld, tail: str = WORLD_TAIL) -> NearbyObject:
    return NearbyObject(ref=f"container:{world.session_id}:{tail}", kind="container", distance=1.5)


def content_result(kind: str, ref_key: str, container_ref: str, *, seq: int) -> ActionResult:
    return ActionResult.succeeded(
        session_id=str(uuid.UUID(int=0x5E5510)),
        seq=seq,
        command_id=str(uuid.uuid4()),
        action="container.inspect",
        timestamp_ms=BASE_TIME_MS,
        evidence=Evidence(
            kind=kind, observation_seq=1, observed={ref_key: container_ref}
        ).to_payload(),
    )


# ---------------------------------------------------------------------------
# the port, and finding the memory behind the planner
# ---------------------------------------------------------------------------


def test_sidecar_memory_satisfies_the_loops_port(tmp_path: Path) -> None:
    """The static and the runtime halves of the structural seam, both pinned."""
    memory = sidecar_memory(tmp_path)

    port: ContainerMemory = memory  # the assignment is the mypy check

    assert isinstance(port, ContainerMemory)


def test_the_loop_finds_the_memory_through_the_navigation_wrapper(tmp_path: Path) -> None:
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))

    assert world.loop._container_memory() is recording


def test_the_loop_finds_the_memory_on_an_unwrapped_planner_too(tmp_path: Path) -> None:
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=MemoryHolder(recording))

    assert world.loop._container_memory() is recording


def test_a_loop_without_a_planner_has_no_memory_to_feed(tmp_path: Path) -> None:
    assert make_sidecar_world(tmp_path).loop._container_memory() is None


def test_a_storeless_fallback_memory_is_not_fed(tmp_path: Path) -> None:
    """NoMemory answers the autonomy questions but records nothing; feeding it
    would silently discard every sighting while looking wired."""
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(NO_MEMORY)))

    assert world.loop._container_memory() is None


# ---------------------------------------------------------------------------
# the tick feeds observations
# ---------------------------------------------------------------------------


def test_a_tick_with_a_fresh_observation_feeds_it_to_the_memory(tmp_path: Path) -> None:
    recording = RecordingMemory()
    with attached_world(tmp_path, planner=Wrapper(MemoryHolder(recording))) as world:
        world.observe()

        world.loop.tick()

        assert recording.observations == [(1, world.clock.now)]


def test_a_tick_that_ingested_nothing_feeds_nothing(tmp_path: Path) -> None:
    """The gate is new observations, not the tick itself: an idle loop must not
    re-offer the same snapshot four times a second."""
    recording = RecordingMemory()
    with attached_world(tmp_path, planner=Wrapper(MemoryHolder(recording))) as world:
        world.observe()
        world.loop.tick()

        world.loop.tick()

        assert len(recording.observations) == 1


def test_a_world_container_seen_by_a_tick_lands_in_the_memory_file(tmp_path: Path) -> None:
    """The whole sighting half, end to end: journal to memory file on disk."""
    memory = sidecar_memory(tmp_path)
    with attached_world(tmp_path, planner=Wrapper(MemoryHolder(memory))) as world:
        world.observe(nearby=NearbyView(objects=[world_container_object(world)]))

        world.loop.tick()

        assert memory.store.path_for(DEFAULT_SAVE).is_file()
        stored = memory.store.load(DEFAULT_SAVE)
        assert [record.tail for record in stored.containers()] == [WORLD_TAIL]
        assert list(stored.uninspected_tails()) == [WORLD_TAIL]


def test_the_memory_file_is_not_written_on_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cadence, pinned through real ticks: dirty per tick, one write per
    NOTE_FLUSH_INTERVAL_MS."""
    memory = sidecar_memory(tmp_path)
    writes: list[int] = []
    real_save = memory.store.save

    def counting_save(saved: object) -> int:
        written = real_save(saved)  # type: ignore[arg-type]
        writes.append(written)
        return written

    monkeypatch.setattr(memory.store, "save", counting_save)
    with attached_world(tmp_path, planner=Wrapper(MemoryHolder(memory))) as world:
        for _ in range(3):
            world.clock.advance(1_000)
            world.beat_game()
            world.observe(nearby=NearbyView(objects=[world_container_object(world)]))
            world.loop.tick()
        assert len(writes) == 1, "a write per tick is the workload the cadence exists to stop"

        world.clock.advance(NOTE_FLUSH_INTERVAL_MS)
        world.beat_game()
        world.observe(nearby=NearbyView(objects=[world_container_object(world)]))
        world.loop.tick()

        assert len(writes) == 2


# ---------------------------------------------------------------------------
# terminal evidence feeds inspections
# ---------------------------------------------------------------------------


def test_each_content_evidence_kind_names_its_container_to_the_memory(tmp_path: Path) -> None:
    """The three kinds and their ref keys, exactly as the adapters spell them."""
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))
    accepted = world.loop.store.push(make_observation(game=make_game()))
    assert accepted.accepted, accepted.detail
    results = (
        content_result("container_contents_described", "container_ref", "container:s:a", seq=1),
        content_result("item_in_destination_container", "container_ref", "container:s:b", seq=2),
        content_result("items_in_destination_container", "destination_ref", "container:s:c", seq=3),
    )

    world.loop._feed_inspections(recording, results, BASE_TIME_MS)

    assert recording.inspections == ["container:s:a", "container:s:b", "container:s:c"]


def test_a_failed_result_with_content_evidence_is_still_fed(tmp_path: Path) -> None:
    """A batch transfer stopped by a full destination ends FAILED while its
    evidence honestly records what was observed landing there."""
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))
    assert world.loop.store.push(make_observation(game=make_game())).accepted
    stopped = ActionResult.failure(
        session_id=str(uuid.UUID(int=0x5E5510)),
        seq=1,
        command_id=str(uuid.uuid4()),
        action="inventory.transfer_batch",
        timestamp_ms=BASE_TIME_MS,
        reason_code=ReasonCode.CONTAINER_FULL,
        status=ActionStatus.FAILED,
        evidence=Evidence(
            kind="items_in_destination_container",
            observation_seq=1,
            observed={"destination_ref": "container:s:crate"},
        ).to_payload(),
    )

    world.loop._feed_inspections(recording, (stopped,), BASE_TIME_MS)

    assert recording.inspections == ["container:s:crate"]


def test_evidence_of_other_shapes_and_kinds_is_ignored(tmp_path: Path) -> None:
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))
    assert world.loop.store.push(make_observation(game=make_game())).accepted
    flat_refusal = ActionResult.failure(
        session_id=str(uuid.UUID(int=0x5E5510)),
        seq=1,
        command_id=str(uuid.uuid4()),
        action="container.inspect",
        timestamp_ms=BASE_TIME_MS,
        reason_code=ReasonCode.PRECONDITION_FAILED,
        # A refusal's evidence is flat — no kind, no observed block.
        evidence={"container_ref": "container:s:a"},
    )
    other_kind = content_result("container_within_reach", "container_ref", "container:s:b", seq=2)

    world.loop._feed_inspections(recording, (flat_refusal, other_kind), BASE_TIME_MS)

    assert recording.inspections == []


def test_no_observation_means_no_inspection_is_offered(tmp_path: Path) -> None:
    """An enumeration nobody observed is not a fact to store, whatever the ack said."""
    recording = RecordingMemory()
    world = make_sidecar_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))
    result = content_result("container_contents_described", "container_ref", "container:s:a", seq=1)

    world.loop._feed_inspections(recording, (result,), BASE_TIME_MS)

    assert recording.inspections == []


# ---------------------------------------------------------------------------
# shutdown flushes what the cadence had not
# ---------------------------------------------------------------------------


def test_shutdown_flushes_pending_notes(tmp_path: Path) -> None:
    recording = RecordingMemory()
    world = attached_world(tmp_path, planner=Wrapper(MemoryHolder(recording)))

    world.loop.shutdown(reason="the test finished")

    assert recording.flushes == 1


def test_a_sighting_younger_than_the_cadence_survives_a_shutdown(tmp_path: Path) -> None:
    """The "and on close" half of the cadence, with the real memory behind it."""
    memory = sidecar_memory(tmp_path)
    with attached_world(tmp_path, planner=Wrapper(MemoryHolder(memory))) as world:
        world.observe(nearby=NearbyView(objects=[world_container_object(world)]))
        world.loop.tick()
        # A second container arrives inside the flush interval, so only the
        # shutdown below can save it.
        world.clock.advance(1_000)
        world.beat_game()
        second = "world:1215:3406:0:1:0"
        world.observe(nearby=NearbyView(objects=[world_container_object(world, second)]))
        world.loop.tick()

        world.loop.shutdown(reason="the test finished")

        stored = memory.store.load(DEFAULT_SAVE)
        assert {record.tail for record in stored.containers()} == {WORLD_TAIL, second}
