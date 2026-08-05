"""The bounded record of what each idempotency key already produced."""

from __future__ import annotations

import pytest

from pz_agent_core.protocol import ReasonCode
from pz_agent_mcp.envelope import ToolFailure
from pz_agent_mcp.idempotency import MAX_CAPACITY, CachedCall, IdempotencyCache


def call(tool: str = "pz_action_eat", action_id: str | None = "act-1") -> CachedCall:
    return CachedCall(tool=tool, payload={"status": "accepted"}, action_id=action_id)


def test_an_unknown_key_is_a_miss() -> None:
    assert IdempotencyCache().lookup("pz_action_eat", "k1") is None


def test_a_known_key_replays_the_recorded_call() -> None:
    cache = IdempotencyCache()
    cache.remember("k1", call())

    found = cache.lookup("pz_action_eat", "k1")

    assert found is not None
    assert found.action_id == "act-1"


def test_the_same_key_on_a_different_tool_is_the_callers_bug() -> None:
    # Answering with the other tool's result would be wrong; ignoring the
    # collision would act twice under a key the client believes is spent.
    cache = IdempotencyCache()
    cache.remember("k1", call(tool="pz_action_eat"))

    with pytest.raises(ToolFailure) as caught:
        cache.lookup("pz_action_drink", "k1")

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "pz_action_eat" in caught.value.message


def test_capacity_is_enforced_and_the_oldest_entry_is_evicted() -> None:
    cache = IdempotencyCache(capacity=3)
    for n in range(5):
        cache.remember(f"k{n}", call())

    assert len(cache) == 3
    assert cache.lookup("pz_action_eat", "k0") is None
    assert cache.lookup("pz_action_eat", "k4") is not None


def test_a_lookup_makes_an_entry_the_most_recently_used() -> None:
    cache = IdempotencyCache(capacity=2)
    cache.remember("k0", call())
    cache.remember("k1", call())

    cache.lookup("pz_action_eat", "k0")
    cache.remember("k2", call())

    assert cache.lookup("pz_action_eat", "k0") is not None
    assert cache.lookup("pz_action_eat", "k1") is None


def test_clear_forgets_every_key() -> None:
    cache = IdempotencyCache()
    cache.remember("k1", call())

    cache.clear()

    assert len(cache) == 0
    assert cache.lookup("pz_action_eat", "k1") is None


@pytest.mark.parametrize("capacity", [0, -1, MAX_CAPACITY + 1])
def test_a_capacity_outside_the_bounds_is_refused(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity must be within"):
        IdempotencyCache(capacity=capacity)
