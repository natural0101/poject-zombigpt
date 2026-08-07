"""The synthetic-input fallback, and the door that is shut in front of it.

§16 keeps an experimental visual/input adapter on the table for actions Lua
timed actions cannot perform, behind a feature flag that is off by default. This
build implements none of it — :func:`test_nothing_in_this_build_can_send_input`
is what keeps that statement true — but "not implemented yet" is not a safety
property, because it stops being true the day somebody implements it.

So the rule lives in one function rather than in the absence of a caller.
:func:`~pz_agent_core.safety.input.evaluate_synthetic_input` is the gate any such
adapter must pass, and the case this file exists for is the user's binding
constraint: **no synthetic input in multiplayer** (E12-M02-T008). A virtual
gamepad or keyboard driving a character on somebody else's server is an input
cheat whatever it was meant to be, and the mod cannot tell the server otherwise.
``None`` is refused exactly as ``True`` is, because an absent reading is not a
negative one — the same rule that stops a missing ``is_bleeding`` from meaning
"not bleeding".

Two gates that already existed are checked alongside it, because the refusal
above is only worth what its neighbours are worth: the session cannot be armed
into ``EXPERIMENTAL_INPUT`` at all, and the action engine re-decides multiplayer
for every command against the observation that command acts on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.runtime import ARMABLE_MODES
from pz_agent_core.actions import ActionEngine, ActionRequest, AdapterRegistry
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    CommandPolicy,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.safety.input import (
    SyntheticInputDecision,
    SyntheticInputRequest,
    evaluate_synthetic_input,
)
from tests.fixtures import DEFAULT_SESSION, make_game, make_observation, make_safety
from tests.fixtures.action_doubles import (
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)

#: The one state in which a synthetic-input adapter would be permitted to act:
#: the mod read the session as single player, the operator turned the
#: experimental adapter on, and the session is in the mode that is about it.
PERMITTED: Final = (SessionMode.EXPERIMENTAL_INPUT, False, True)

MULTIPLAYER_READINGS: Final[tuple[bool | None, ...]] = (True, None)


def _decide(
    mode: SessionMode = SessionMode.EXPERIMENTAL_INPUT,
    multiplayer: bool | None = False,
    *,
    enabled: bool = True,
) -> SyntheticInputDecision:
    return evaluate_synthetic_input(
        SyntheticInputRequest(mode=mode, multiplayer=multiplayer, enabled=enabled)
    )


# ---------------------------------------------------------------------------
# the refusal this task is about
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(SessionMode), ids=lambda mode: mode.value)
@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
@pytest.mark.parametrize("multiplayer", MULTIPLAYER_READINGS, ids=["multiplayer", "unknown"])
def test_multiplayer_refuses_synthetic_input_in_every_mode_and_however_it_is_configured(
    mode: SessionMode, enabled: bool, multiplayer: bool | None
) -> None:
    decision = _decide(mode, multiplayer, enabled=enabled)

    assert not decision.allowed
    assert decision.reason_code is ReasonCode.POLICY_DENIED
    assert "multiplayer" in decision.detail


def test_the_only_state_in_which_synthetic_input_is_permitted_is_stated_in_full() -> None:
    """A truth table, so the gate can fail in both directions.

    Written out rather than derived: a test that computed the expected answer
    from the same condition the gate uses would pass whatever that condition
    said.
    """
    allowed = {
        (mode, multiplayer, enabled)
        for mode in SessionMode
        for multiplayer in (True, False, None)
        for enabled in (True, False)
        if _decide(mode, multiplayer, enabled=enabled).allowed
    }

    assert allowed == {PERMITTED}


def test_the_feature_flag_is_off_unless_it_is_set() -> None:
    """§16.2: ``enabled = false`` is the shipped default, so it is the default here."""
    request = SyntheticInputRequest(mode=SessionMode.EXPERIMENTAL_INPUT, multiplayer=False)

    assert request.enabled is False
    decision = evaluate_synthetic_input(request)
    assert not decision.allowed
    assert "not enabled" in decision.detail


def test_a_session_in_another_mode_is_refused_even_with_the_flag_on() -> None:
    decision = _decide(SessionMode.AUTONOMOUS, False, enabled=True)

    assert not decision.allowed
    assert SessionMode.EXPERIMENTAL_INPUT.value in decision.detail


def test_a_refusal_always_carries_a_code_and_an_allowance_never_does() -> None:
    """The shape callers report; a decision that explains nothing is a defect."""
    with pytest.raises(ValueError, match="reason code"):
        SyntheticInputDecision(allowed=False, detail="refused for no stated reason")
    with pytest.raises(ValueError, match="reason code"):
        SyntheticInputDecision(
            allowed=True, detail="permitted", reason_code=ReasonCode.POLICY_DENIED
        )
    with pytest.raises(ValueError, match="explain"):
        SyntheticInputDecision(allowed=True, detail="   ")


# ---------------------------------------------------------------------------
# the reading comes from the mod, never from the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplayer", MULTIPLAYER_READINGS, ids=["multiplayer", "unknown"])
def test_the_request_takes_the_session_from_the_observation(multiplayer: bool | None) -> None:
    """So no caller can supply its own opinion of who else is in the world."""
    observation = make_observation(
        game=make_game(multiplayer=multiplayer),
        safety=make_safety(mode=SessionMode.EXPERIMENTAL_INPUT, armed=True),
    )

    request = SyntheticInputRequest.from_observation(observation, enabled=True)

    assert request.multiplayer is multiplayer
    assert request.mode is SessionMode.EXPERIMENTAL_INPUT
    assert not evaluate_synthetic_input(request).allowed


def test_a_single_player_observation_reaches_the_permitted_state() -> None:
    """The control: ``from_observation`` is not simply refusing everything."""
    observation = make_observation(
        game=make_game(multiplayer=False),
        safety=make_safety(mode=SessionMode.EXPERIMENTAL_INPUT, armed=True),
    )

    assert evaluate_synthetic_input(
        SyntheticInputRequest.from_observation(observation, enabled=True)
    ).allowed


# ---------------------------------------------------------------------------
# the gates that already stood here
# ---------------------------------------------------------------------------

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Libraries and OS entry points by which a process sends input to another
#: window. None of them may appear in shipped source: the gate above is only the
#: only door while there is nothing behind a second one.
INPUT_APIS: Final[tuple[str, ...]] = (
    "pyautogui",
    "pydirectinput",
    "sendinput",
    "keybd_event",
    "mouse_event",
    "vigem",
    "vgamepad",
    "xtestfake",
    "win32api",
    "user32",
)


def test_nothing_in_this_build_can_send_input() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}: {api}"
        for root in ("packages", "pz-mod", "installer")
        for path in sorted((REPO_ROOT / root).rglob("*"))
        if path.suffix in {".py", ".lua"} and "__pycache__" not in path.parts
        for api in INPUT_APIS
        if api in path.read_text(encoding="utf-8").lower()
    ]

    assert found == [], f"these look like synthetic input: {found}"


def test_no_session_can_be_armed_into_the_mode_that_would_use_it() -> None:
    """The loop grants ASSISTED and AUTONOMOUS and nothing else."""
    assert SessionMode.EXPERIMENTAL_INPUT not in ARMABLE_MODES
    assert sorted(mode.value for mode in ARMABLE_MODES) == ["ASSISTED", "AUTONOMOUS"]


@pytest.mark.parametrize("multiplayer", MULTIPLAYER_READINGS, ids=["multiplayer", "unknown"])
def test_the_engine_refuses_a_command_in_a_multiplayer_session_in_this_mode_too(
    multiplayer: bool | None,
) -> None:
    """The second gate, re-decided per command against the fresh observation.

    ``EXPERIMENTAL_INPUT`` is a mutating mode, so the engine is where a session
    that went multiplayer after arming is caught — and it is caught here whether
    or not any input adapter exists.
    """
    world = make_observation(
        game=make_game(multiplayer=multiplayer),
        safety=make_safety(mode=SessionMode.EXPERIMENTAL_INPUT, armed=True),
    )
    engine, sink = _engine(world)

    result = engine.execute(_request(ActionName.MOVEMENT_MOVE_TO))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.POLICY_DENIED
    assert sink.sent == []


def test_the_same_command_runs_when_the_mod_says_single_player() -> None:
    world = make_observation(
        game=make_game(multiplayer=False),
        safety=make_safety(mode=SessionMode.EXPERIMENTAL_INPUT, armed=True),
    )
    engine, sink = _engine(world)

    result = engine.execute(_request(ActionName.MOVEMENT_MOVE_TO))

    assert result.reason_code is not ReasonCode.POLICY_DENIED
    assert [command.action for command in sink.sent] == [ActionName.MOVEMENT_MOVE_TO]


def _engine(world: Observation) -> tuple[ActionEngine, FakeCommandSink]:
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(world)
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _: True,
    )
    return engine, sink


def _request(action: ActionName) -> ActionRequest:
    return ActionRequest(
        action=action,
        session_id=DEFAULT_SESSION,
        idempotency_key=f"{action.value}-1",
        args={"target": {"x": 1, "y": 2, "z": 0}},
        policy=CommandPolicy(allow_interrupt=True, max_retries=0),
    )
