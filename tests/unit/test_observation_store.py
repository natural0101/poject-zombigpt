"""The store: bounded history, and a gap it refuses to paper over."""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.observation.store import (
    DEFAULT_WINDOW,
    MAX_WINDOW,
    ObservationStore,
)
from tests.fixtures import make_observation


def test_a_store_starts_empty_and_asks_for_a_full_snapshot() -> None:
    store = ObservationStore()

    assert store.latest() is None
    assert store.previous() is None
    assert store.window(4) == ()
    assert store.needs_full_snapshot


def test_a_partial_observation_cannot_establish_the_baseline() -> None:
    store = ObservationStore()
    result = store.push(make_observation(seq=1, full=False))

    assert not result.accepted
    assert result.full_snapshot_required
    assert store.latest() is None
    assert store.rejected_count == 1


def test_consecutive_observations_are_accepted_in_order() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    result = store.push(make_observation(seq=2, full=False))

    assert result.accepted
    assert result.gap is None
    assert store.latest() is not None
    assert store.latest().seq == 2  # type: ignore[union-attr]
    assert store.previous().seq == 1  # type: ignore[union-attr]


def test_the_ring_never_grows_past_its_capacity() -> None:
    store = ObservationStore(capacity=4)
    for seq in range(1, 40):
        store.push(make_observation(seq=seq))

    assert store.size == 4
    assert store.capacity == 4
    assert len(store.window(100)) == 4
    assert [o.seq for o in store.window(100)] == [39, 38, 37, 36]


def test_the_default_window_is_bounded_too() -> None:
    store = ObservationStore()
    for seq in range(1, DEFAULT_WINDOW * 3):
        store.push(make_observation(seq=seq))

    assert store.size == DEFAULT_WINDOW


@pytest.mark.parametrize("capacity", [0, 1, MAX_WINDOW + 1, -5])
def test_an_out_of_range_capacity_is_refused(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity must be within"):
        ObservationStore(capacity=capacity)


def test_window_rejects_a_non_positive_size() -> None:
    store = ObservationStore()
    with pytest.raises(ValueError, match="must be positive"):
        store.window(0)


def test_window_returns_newest_first_and_clamps_to_what_it_holds() -> None:
    store = ObservationStore(capacity=8)
    for seq in range(1, 4):
        store.push(make_observation(seq=seq))

    assert [o.seq for o in store.window(2)] == [3, 2]
    assert [o.seq for o in store.window(99)] == [3, 2, 1]


def test_a_gap_followed_by_a_partial_update_is_refused_not_interpolated() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    result = store.push(make_observation(seq=5, full=False))

    assert not result.accepted
    assert result.full_snapshot_required
    assert store.needs_full_snapshot
    assert result.gap is not None
    assert (result.gap.expected, result.gap.received, result.gap.missing) == (2, 5, 3)
    assert store.latest().seq == 1  # type: ignore[union-attr]


def test_a_full_snapshot_resynchronises_after_a_gap() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    store.push(make_observation(seq=5, full=False))
    result = store.push(make_observation(seq=6, full=True))

    assert result.accepted
    assert result.gap is not None
    assert not store.needs_full_snapshot
    assert store.latest().seq == 6  # type: ignore[union-attr]


def test_a_replayed_observation_is_refused() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=4))
    for seq in (4, 3, 1):
        assert not store.push(make_observation(seq=seq)).accepted
    assert store.latest().seq == 4  # type: ignore[union-attr]
    assert store.rejected_count == 3


def test_a_new_session_discards_history_and_demands_a_full_snapshot() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    store.push(make_observation(seq=2))
    other = str(uuid.UUID(int=0xBEEF))

    refused = store.push(make_observation(session_id=other, seq=3, full=False))
    assert not refused.accepted
    assert refused.session_changed
    assert store.needs_full_snapshot
    assert store.latest() is None

    # The change is reported once, on the tick it is detected; by the time the
    # replacement snapshot lands the store has no old session left to compare to.
    accepted = store.push(make_observation(session_id=other, seq=3, full=True))
    assert accepted.accepted
    assert store.session_id == other
    # History from the previous session is gone: its refs no longer resolve.
    assert store.size == 1


def test_a_new_session_that_opens_with_a_full_snapshot_is_accepted() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=9))
    other = str(uuid.UUID(int=0xBEEF))

    result = store.push(make_observation(session_id=other, seq=1, full=True))

    assert result.accepted
    assert result.session_changed
    assert store.latest().seq == 1  # type: ignore[union-attr]


def test_reset_forgets_everything() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    store.reset()

    assert store.latest() is None
    assert store.needs_full_snapshot
    assert store.session_id is None


def test_the_last_gap_is_kept_for_diagnostics() -> None:
    store = ObservationStore()
    store.push(make_observation(seq=1))
    store.push(make_observation(seq=9, full=True))

    assert store.last_gap is not None
    assert store.last_gap.missing == 7
