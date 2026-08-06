"""The blueprint forbids multiplayer. This is the test that it is forbidden.

It was not. `safety.allow_multiplayer` sat in ``config._advisories``, whose
docstring promises "Never errors", and carried the sentence *"multiplayer is
refused at the handshake regardless of this setting"*. There was no such
refusal. Not in ``evaluate_handshake``, not in ``Session.lua``, not anywhere in
``packages/`` or ``pz-mod/`` — a grep for "multiplayer" across both found the
advisory's own text and two unrelated comments about ``getOnlineID`` returning
-1 outside multiplayer. So the warning described a gate nobody had written, and
the setting was precisely the bypass it claimed not to be: turn it on, read a
line of advice, proceed.

Two gates now, and this file exercises both, because they protect different
people. The configuration refusal protects whoever reads the file. The engine
refusal protects whoever does not — and it is the load-bearing one, because it
re-decides for every command against the observation that command is acting on,
where an arm-time check would be blind to a session that changed underneath it.

The third state is the point. ``multiplayer`` is True, False, or **absent**, and
absent is refused exactly as True is. That is not caution for its own sake: it
is the rule this project applies everywhere else, where a missing
``is_bleeding`` never means "not bleeding" and a missing capability never means
"available". A safety gate is the last place to start reading silence as
permission.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.config import SCHEMA, load_config
from pz_agent_core.actions import ActionEngine, ActionRequest, AdapterRegistry
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    CommandPolicy,
    Observation,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION, make_game, make_observation
from tests.fixtures.action_doubles import (
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)

#: Every action that must still work in a session the agent refuses to act in.
#: Stopping and cancelling because that is how a person takes control back;
#: the readings because looking at a world changes nothing in it.
STILL_ALLOWED: Final = (
    ActionName.SAFETY_STOP,
    ActionName.SESSION_DISARM,
    ActionName.PLAN_CANCEL,
    ActionName.WORLD_INSPECT,
    ActionName.CONTAINER_INSPECT,
    ActionName.INVENTORY_SEARCH,
    ActionName.ACTION_WAIT,
)


def _world(multiplayer: bool | None) -> Observation:
    """An observation whose only interesting property is who else is in it."""
    base = make_observation()
    return replace(base, game=make_game(multiplayer=multiplayer))


@dataclass
class _Rig:
    engine: ActionEngine
    sink: FakeCommandSink


def _rig(world: Observation, *, action: ActionName = ActionName.MOVEMENT_MOVE_TO) -> _Rig:
    """A real ActionEngine over doubles, assembled here rather than borrowed.

    A safety test that imported its harness from a unit-test module would fail
    for reasons belonging to that module. Everything below is the production
    engine; only the clock, the sink and the observation source are doubles, and
    the capability check is open so that a refusal can only come from the gate
    under test.
    """
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(world)
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=action))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        panic_stop=lambda: False,
        capability_check=lambda _: True,
    )
    return _Rig(engine=engine, sink=sink)


def _request(action: ActionName) -> ActionRequest:
    return ActionRequest(
        action=action,
        session_id=DEFAULT_SESSION,
        idempotency_key=f"{action.value}-1",
        args={"target": {"x": 1, "y": 2, "z": 0}},
        policy=CommandPolicy(allow_interrupt=True, max_retries=0),
    )


# ---------------------------------------------------------------------------
# the configuration gate
# ---------------------------------------------------------------------------


def test_the_configuration_refuses_the_setting_instead_of_advising_about_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[safety]\nallow_multiplayer = true\n", encoding="utf-8")

    validation = load_config(path)

    assert not validation.ok
    assert validation.config is None, "a refused configuration must not be handed back"
    named = [problem for problem in validation.errors if problem.path == "safety.allow_multiplayer"]
    assert len(named) == 1
    assert named[0].remediation


def test_no_other_setting_can_turn_the_refusal_off() -> None:
    """A gate with a switch beside it is not a gate."""
    escapes = sorted(
        f"{table}.{key}"
        for table, keys in SCHEMA.items()
        for key in keys
        if key != "allow_multiplayer"
        and any(word in key for word in ("multiplayer", "unsafe", "override", "bypass", "force"))
    )
    assert escapes == [], f"these look like ways around the refusal: {escapes}"


# ---------------------------------------------------------------------------
# the engine gate — the one that actually protects a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplayer", [True, None], ids=["multiplayer", "unknown"])
def test_a_mutating_command_is_refused(multiplayer: bool | None) -> None:
    rig = _rig(_world(multiplayer))

    result = rig.engine.execute(_request(ActionName.MOVEMENT_MOVE_TO))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.POLICY_DENIED
    # `succeeded` is a constructor on ActionResult, not a property, so
    # `assert not result.succeeded` would pass on any result at all. The status
    # above is the real check; this one names the thing that matters most.
    assert rig.sink.sent == [], "nothing may reach the mod"


def test_a_mutating_command_runs_when_the_mod_says_single_player() -> None:
    """The other half: the gate must not be refusing everything.

    Without this, a bug that refused unconditionally would leave the two
    assertions above passing and the product dead.
    """
    rig = _rig(_world(False))

    result = rig.engine.execute(_request(ActionName.MOVEMENT_MOVE_TO))

    assert result.reason_code is not ReasonCode.POLICY_DENIED
    assert rig.sink.sent, "a single-player command must reach the mod"


@pytest.mark.parametrize("multiplayer", [True, None], ids=["multiplayer", "unknown"])
@pytest.mark.parametrize("action", STILL_ALLOWED, ids=lambda a: a.value)
def test_stopping_and_looking_still_work(action: ActionName, multiplayer: bool | None) -> None:
    """Refusing to act must never become refusing to stop.

    An agent that cannot be stopped in the one session it should not be running
    in is worse than one that never had the gate.
    """
    rig = _rig(_world(multiplayer), action=action)

    result = rig.engine.execute(_request(action))

    assert result.reason_code is not ReasonCode.POLICY_DENIED, (
        f"{action.value} was refused for multiplayer; it must not be"
    )


# ---------------------------------------------------------------------------
# the protocol carries the three states
# ---------------------------------------------------------------------------


def test_absent_survives_the_wire_as_absent_rather_than_false() -> None:
    """The whole gate rests on this distinction, so it is checked directly.

    A ``to_dict`` that wrote ``multiplayer: false`` for an unread value would
    hand out the permission the gate exists to withhold, and every assertion
    above would still pass.
    """
    unknown = make_game(multiplayer=None)
    assert "multiplayer" not in unknown.to_dict()
    assert unknown.multiplayer is None
    assert not unknown.provably_single_player

    single = make_game(multiplayer=False)
    assert single.to_dict()["multiplayer"] is False
    assert single.provably_single_player

    both = make_game(multiplayer=True)
    assert both.to_dict()["multiplayer"] is True
    assert not both.provably_single_player
