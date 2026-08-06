"""Builders for the permission, autonomy and memory tests (T022/T024).

The shared builders produce a calm, healthy, ``ASSISTED`` session. Autonomy only
ever runs in an armed ``AUTONOMOUS`` one with a verified backup, so composing
that state by hand in every test would bury the field under assertion in ten
lines of scaffolding — and a test that forgot one of them would pass for the
wrong reason, because the gate refuses on any of them.
"""

from __future__ import annotations

from typing import Any

from pz_agent_core.memory import SaveMemory, Square
from pz_agent_core.policy.autonomy import BackupEvidence, HomeSquare
from pz_agent_core.protocol import (
    ContainerKind,
    DangerLevel,
    Observation,
    PlayerState,
    SessionMode,
)
from tests.fixtures import (
    DEFAULT_SAVE,
    DEFAULT_SESSION,
    make_observation,
    make_player,
    make_safety,
)

#: A world container reference in the standard session, at the standard square.
WORLD_CONTAINER_REF = f"container:{DEFAULT_SESSION}:world:1200:3400:0:1:0"

NOW_MS = 1_700_000_000_000


def world_container_ref(x: int = 1200, y: int = 3400, z: int = 0, index: int = 1) -> str:
    return f"container:{DEFAULT_SESSION}:world:{x}:{y}:{z}:{index}:0"


def autonomous_observation(**overrides: Any) -> Observation:
    """An observation in which the autonomy gate's session checks all pass."""
    safety = overrides.pop(
        "safety",
        make_safety(mode=SessionMode.AUTONOMOUS, armed=True, danger_level=DangerLevel.NONE),
    )
    return make_observation(safety=safety, **overrides)


def calm_player(**overrides: Any) -> PlayerState:
    """A player with no need above its trigger, before the test adds one.

    Keyword arguments that name a :class:`PlayerState` field are passed through;
    everything else is read as a stat, which is what most callers here want.
    """
    fields = {key: overrides.pop(key) for key in _PLAYER_FIELDS if key in overrides}
    stats: dict[str, Any] = {"hunger": 0.1, "thirst": 0.1, "fatigue": 0.1, "endurance": 0.9}
    stats.update(overrides.pop("stats", {}))
    stats.update(overrides)
    return make_player(stats=stats, **fields)


_PLAYER_FIELDS = ("present", "alive", "position", "moodles", "wounds", "hands")


def verified_backup(save_id: str = DEFAULT_SAVE, created_at_ms: int = NOW_MS) -> BackupEvidence:
    return BackupEvidence(save_id=save_id, created_at_ms=created_at_ms, verified=True)


class FakeMemory:
    """The two answers the autonomy gate needs, without a store behind them."""

    def __init__(
        self,
        *,
        home: Square | None = None,
        reserved: frozenset[str] = frozenset(),
    ) -> None:
        self._home = home
        self._reserved = reserved

    def home_point(self) -> HomeSquare | None:
        return self._home

    def reserves_item(self, full_type: str, /) -> bool:
        return full_type in self._reserved


def memory_with_containers(
    save_id: str,
    count: int,
    *,
    now_ms: int = NOW_MS,
    step_ms: int = 1_000,
    **kwargs: Any,
) -> SaveMemory:
    """A memory holding *count* distinct world containers, each seen later than the last."""
    memory = SaveMemory(save_id, **kwargs)
    for index in range(count):
        memory.note_container(
            container_ref=world_container_ref(x=1200 + index),
            kind=ContainerKind.WORLD,
            name=f"Shelf {index}",
            now_ms=now_ms + index * step_ms,
        )
    return memory
