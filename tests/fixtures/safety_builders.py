"""Builders for the safety and observation tests.

The shared builders in :mod:`tests.fixtures` produce a calm, healthy baseline.
These add the pieces those tests need to make it go wrong: zombies at chosen
distances, wounds that bleed, and in-flight commands with chosen deadlines.
"""

from __future__ import annotations

from typing import Any

from pz_agent_core.protocol import (
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Position,
    Wound,
)
from pz_agent_core.safety.reflex import InFlightCommand
from tests.fixtures import DEFAULT_SESSION


def zombie_ref(runtime_id: str, session_id: str = DEFAULT_SESSION) -> str:
    return f"zombie:{session_id}:{runtime_id}:0"


def object_ref(runtime_id: str, session_id: str = DEFAULT_SESSION) -> str:
    return f"object:{session_id}:{runtime_id}:0"


def make_zombie(
    runtime_id: str,
    distance: float,
    *,
    # Tri-state, like the field: ``None`` is "the build could not report the
    # zombie's target", which the threat assessment must not read as calm.
    chasing: bool | None = False,
    visible: bool = True,
    floor: int | None = None,
) -> NearbyZombie:
    position = None if floor is None else Position(x=1200.0, y=3400.0, z=floor)
    return NearbyZombie(
        ref=zombie_ref(runtime_id),
        distance=distance,
        visible=visible,
        chasing=chasing,
        position=position,
    )


def make_object(runtime_id: str, distance: float, **overrides: Any) -> NearbyObject:
    base: dict[str, Any] = {
        "ref": object_ref(runtime_id),
        "kind": "container",
        "distance": distance,
        "semantics": ["storage"],
    }
    base.update(overrides)
    return NearbyObject(**base)


def make_nearby(*zombies: NearbyZombie, objects: tuple[NearbyObject, ...] = ()) -> NearbyView:
    return NearbyView(objects=list(objects), zombies=list(zombies))


def bleeding_wound(severity: float = 0.6) -> Wound:
    return Wound(
        ref=f"wound:{DEFAULT_SESSION}:arm-l", kind="laceration", severity=severity, bleeding=True
    )


#: The "now" every safety test shares, so a fixture default and a test-supplied
#: timestamp cannot silently disagree about which side of a deadline they are on.
NOW_MS = 1_700_000_000_000


def in_flight(
    command_id: str = "cmd-1",
    *,
    action: str = "consume.eat",
    deadline_ms: int = NOW_MS + 30_000,
    last_progress_ms: int = NOW_MS,
    moves_character: bool = False,
) -> InFlightCommand:
    return InFlightCommand(
        command_id=command_id,
        action=action,
        deadline_ms=deadline_ms,
        last_progress_ms=last_progress_ms,
        moves_character=moves_character,
    )
