"""The voice command's own branches, below the seam test.

``tests/contract/test_voice_wiring.py`` proves a spoken word reaches the game on
a machine where everything is where it should be. These are the readings that
only happen when it is not: an exchange directory nothing is beating in, a
heartbeat that has gone quiet, a record written by another version, and every
way ``voice run`` refuses before it starts anything.

The session port is the subject of most of them, because it is the one piece
here that turns files into a claim about the world. Every one of its answers is
checked in the direction that costs something: an absent heartbeat must read as
disconnected rather than as calm, and a heartbeat that does not mention arming
must read as disarmed rather than as unknown-and-therefore-fine.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.config import ADAPTER_NONE, ADAPTER_TEAMON, AgentConfig, default_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from pz_agent_cli.supervisor import CONTROL_FILE_NAME, SidecarSupervisor
from pz_agent_cli.voice import (
    GOALS_UNROUTED,
    VOICE_RECORD_NAME,
    ExchangeSessionPort,
    GoalUnroutable,
    UnroutedPlanPort,
    VoiceRecord,
    VoiceRecordError,
    VoiceRefused,
    adapter_name,
    collect_voice_status,
    publish_voice_record,
    read_phrase,
    read_voice_record,
    run_voice_run,
    select_adapter,
    voice_services,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.planner.providers import DEFAULT_TEAMON_KEY_ENV
from pz_agent_core.protocol import DangerLevel, SessionMode
from pz_agent_core.session.heartbeat import DEFAULT_TIMEOUT_MS, HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.adapters.teamon import TeamONTranscript, TeamONVoiceAdapter
from pz_agent_voice.messages import MAX_TRANSCRIPT_CHARS, VoiceGoal, VoiceIntent
from pz_agent_voice.ports import PlanRequest
from tests.fixtures.cli_worlds import make_world
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.voice_doubles import FakeTeamONClient

BUILD: Final = "42.20"

KEY: Final = "teamon-key-for-tests"


def a_port(tmp_path: Path, *, clock: FakeClock) -> ExchangeSessionPort:
    """A session port over an empty exchange directory."""
    layout = IpcLayout(tmp_path / "pz_agent")
    layout.ensure()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return ExchangeSessionPort(
        layout=layout,
        supervisor=SidecarSupervisor(state_dir, clock=clock),
        clock=clock,
    )


def beat(port: ExchangeSessionPort, *, clock: FakeClock, **fields: object) -> None:
    """Publish a game heartbeat the way the mod does."""
    monitor = HeartbeatMonitor(port.layout, clock=clock)
    monitor.publish(
        Peer.GAME,
        session_id=str(uuid.UUID(int=0x5E5510)),
        nonce=uuid.uuid4().hex,
        version=PRODUCT_VERSION,
        build=BUILD,
        player_present=True,
        **fields,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# the session port's readings
# ---------------------------------------------------------------------------


def test_an_exchange_directory_nobody_beats_in_is_not_connected(tmp_path: Path) -> None:
    """Absent evidence is never read as a healthy session."""
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)

    snapshot = port.status()

    assert snapshot.connected is False
    assert snapshot.armed is False
    assert snapshot.mode is SessionMode.OBSERVE
    assert snapshot.session_id == ""


def test_a_heartbeat_that_has_gone_quiet_is_not_connected_either(tmp_path: Path) -> None:
    """A game on the main menu and a game that crashed look the same from here."""
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    beat(port, clock=clock, armed=True, mode=SessionMode.ASSISTED)
    clock.advance(DEFAULT_TIMEOUT_MS + 1)

    snapshot = port.status()

    assert snapshot.connected is False
    # Still reported as armed, because that is what the last heartbeat said and
    # this port does not invent a transition nobody observed.
    assert snapshot.armed is True


def test_a_live_heartbeat_is_reported_field_for_field(tmp_path: Path) -> None:
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    beat(
        port,
        clock=clock,
        armed=True,
        mode=SessionMode.AUTONOMOUS,
        danger_level=DangerLevel.HIGH,
        active_action_id="action-1",
    )

    snapshot = port.status()

    assert snapshot.connected is True
    assert snapshot.armed is True
    assert snapshot.mode is SessionMode.AUTONOMOUS
    assert snapshot.danger_level is DangerLevel.HIGH
    assert snapshot.build == BUILD
    assert snapshot.active_action_id == "action-1"
    assert snapshot.game_heartbeat_ok is True
    assert snapshot.sidecar_heartbeat_ok is False


def test_a_heartbeat_that_does_not_mention_arming_reads_as_disarmed(tmp_path: Path) -> None:
    """An omission is not a claim, and the safe reading of one is "no authority"."""
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    beat(port, clock=clock)

    assert port.status().armed is False


# ---------------------------------------------------------------------------
# the stop lever
# ---------------------------------------------------------------------------


def test_the_stop_writes_a_latch_the_mod_would_act_on(tmp_path: Path) -> None:
    """Any non-empty content is a stop; this asserts there is content at all."""
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    beat(port, clock=clock, armed=True, mode=SessionMode.ASSISTED)

    report = port.stop()

    latch = port.layout.panic_stop
    assert latch.is_file()
    assert latch.read_text(encoding="utf-8").strip()
    assert report.disarmed is False, "the game had not reported a disarm yet"
    assert report.mode is SessionMode.ASSISTED


def test_the_stop_reports_a_disarm_it_can_actually_see(tmp_path: Path) -> None:
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    beat(port, clock=clock, armed=False, mode=SessionMode.OBSERVE)

    report = port.stop()

    assert report.disarmed is True
    assert report.cleared == 0, "this side does not invent a count of what the mod cleared"


def test_the_stop_works_with_nothing_running_at_all(tmp_path: Path) -> None:
    """No sidecar, no heartbeat, no session — and the latch still goes down.

    This is the state a user reaches for the stop word in most often: something
    is wrong. A stop route through the control channel would need a running
    sidecar to consume it and would do nothing here.
    """
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)

    report = port.stop()

    assert port.layout.panic_stop.read_text(encoding="utf-8").strip()
    assert report.disarmed is True


def test_a_latch_that_cannot_be_written_raises_rather_than_reporting_a_stop(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    port.layout.panic_stop.mkdir()

    with pytest.raises(OSError):
        port.stop()


def test_arming_is_refused_because_a_room_cannot_grant_authority(tmp_path: Path) -> None:
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)

    with pytest.raises(VoiceRefused, match="pz-agent arm"):
        port.arm(SessionMode.AUTONOMOUS, confirm_backup=True)


def test_disarming_publishes_the_control_request_the_cli_publishes(tmp_path: Path) -> None:
    """One route into the loop's arming state, not two."""
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)

    port.disarm()

    payload = json.loads(
        (port.supervisor.state_dir / CONTROL_FILE_NAME).read_text(encoding="utf-8")
    )
    assert payload["kind"] == "disarm"


# ---------------------------------------------------------------------------
# the plan route that does not exist
# ---------------------------------------------------------------------------


def test_a_goal_is_refused_rather_than_answered_with_a_plan_nobody_made() -> None:
    port = UnroutedPlanPort(detail=GOALS_UNROUTED)
    request = PlanRequest(
        goal=VoiceGoal.EAT.value,
        mode=SessionMode.ASSISTED,
        max_steps=4,
        max_real_seconds=60,
        idempotency_key="key-1",
    )

    with pytest.raises(GoalUnroutable, match="eat"):
        port.execute(request)


def test_no_plan_can_be_read_from_a_process_that_holds_none() -> None:
    """``None`` would be a claim that nothing is running, which is not observable."""
    with pytest.raises(GoalUnroutable):
        UnroutedPlanPort(detail=GOALS_UNROUTED).current()


def test_services_refuse_a_machine_with_no_exchange_directory(tmp_path: Path) -> None:
    world = make_world(tmp_path, with_user_dir=False, with_game=False)
    workspace = resolve_workspace(world.ctx)

    with pytest.raises(VoiceRefused, match="no Zomboid directory"):
        voice_services(workspace, clock=world.ctx.clock_ms)


# ---------------------------------------------------------------------------
# what a phrase resolves to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("стоп", VoiceIntent.STOP),
        ("хватит", VoiceIntent.STOP),
        ("stop", VoiceIntent.STOP),
        ("агент, поешь", VoiceIntent.GOAL),
        ("агент, статус", VoiceIntent.STATUS),
        ("агент", VoiceIntent.WAKE),
        ("агент, поешь и попей", VoiceIntent.AMBIGUOUS),
        ("бармаглот", VoiceIntent.UNKNOWN),
    ],
)
def test_a_phrase_resolves_to_the_intent_the_session_would_reach(
    phrase: str, intent: VoiceIntent
) -> None:
    assert read_phrase(phrase).intent is intent


def test_a_stop_is_never_told_it_needs_a_wake_word() -> None:
    """It does not, from any state, and saying otherwise would be advice to fail on."""
    reading = read_phrase("стоп")

    assert reading.woke is False
    assert not any("wake word" in note for note in reading.notes)


def test_a_goal_is_told_that_nothing_will_route_it() -> None:
    reading = read_phrase("агент, поешь")

    assert reading.goal is VoiceGoal.EAT
    assert any(note == GOALS_UNROUTED for note in reading.notes)


def test_a_phrase_that_matches_nothing_is_not_blamed_on_the_wake_word() -> None:
    """It would have matched nothing with one, and saying otherwise misdirects."""
    reading = read_phrase("бармаглот")

    assert reading.recognised is False
    assert reading.notes == ()


def test_an_over_long_phrase_is_bounded_before_it_is_matched() -> None:
    """The matcher is linear in the transcript, so the transcript is capped first."""
    reading = read_phrase("ля " * 1000 + "стоп")

    assert reading.intent is not VoiceIntent.STOP, "the cap did not apply"
    assert "стоп" not in reading.words, "a word past the cap was matched"
    assert len(" ".join(reading.words)) <= MAX_TRANSCRIPT_CHARS


# ---------------------------------------------------------------------------
# selecting an adapter
# ---------------------------------------------------------------------------


def voice_config(**voice: object) -> AgentConfig:
    """A validated-shaped configuration with one ``[voice]`` section spelled out."""
    section = {"enabled": True, "adapter": ADAPTER_TEAMON, "api_key_env": DEFAULT_TEAMON_KEY_ENV}
    section.update(voice)
    return AgentConfig(values={**default_config().values, "voice": section})


def test_adapter_none_is_refused_rather_than_constructed() -> None:
    with pytest.raises(VoiceRefused, match="nothing to listen with"):
        select_adapter(
            voice_config(adapter=ADAPTER_NONE),
            env={DEFAULT_TEAMON_KEY_ENV: KEY},
            clock=lambda: 0,
        )


def test_a_missing_key_is_refused_by_the_name_of_its_variable() -> None:
    with pytest.raises(VoiceRefused, match=DEFAULT_TEAMON_KEY_ENV):
        select_adapter(voice_config(), env={}, clock=lambda: 0)


def test_a_key_that_is_not_a_variable_name_is_refused_before_anything_starts() -> None:
    """A pasted key in ``api_key_env`` is the mistake this shape check exists for."""
    with pytest.raises(VoiceRefused):
        select_adapter(
            voice_config(api_key_env="sk-live-abc"),
            env={"sk-live-abc": KEY},
            clock=lambda: 0,
        )


def test_teamon_without_a_client_refuses_and_names_the_missing_step() -> None:
    with pytest.raises(VoiceRefused, match="Nothing was started"):
        select_adapter(voice_config(), env={DEFAULT_TEAMON_KEY_ENV: KEY}, clock=lambda: 0)


def test_teamon_with_a_client_is_the_adapter_that_gets_built() -> None:
    adapter = select_adapter(
        voice_config(),
        env={DEFAULT_TEAMON_KEY_ENV: KEY},
        clock=lambda: 0,
        client=FakeTeamONClient(),
    )

    assert isinstance(adapter, TeamONVoiceAdapter)
    assert adapter_name(adapter) == ADAPTER_TEAMON


def test_an_adapter_status_does_not_know_prints_its_own_name() -> None:
    """So a record can never call something the configured adapter's name."""
    assert adapter_name(FakeVoiceAdapter(clock=lambda: 0)) == "FakeVoiceAdapter"


# ---------------------------------------------------------------------------
# the record on disk
# ---------------------------------------------------------------------------


def a_record(**fields: object) -> VoiceRecord:
    values: dict[str, object] = {
        "running": True,
        "adapter": ADAPTER_TEAMON,
        "detail": "a companion is listening on teamon",
        "pid": 4242,
        "started_at_ms": 1_700_000_000_000,
    }
    values.update(fields)
    return VoiceRecord(**values)  # type: ignore[arg-type]


def test_a_record_survives_a_round_trip_through_the_state_directory(tmp_path: Path) -> None:
    path = tmp_path / VOICE_RECORD_NAME

    written = publish_voice_record(path, a_record())

    assert read_voice_record(path) == written


def test_a_record_must_say_how_it_reached_the_state_it_names() -> None:
    with pytest.raises(VoiceRecordError, match="how it reached"):
        a_record(detail="  ")


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({}, "nothing says whether anything is running"),
        ({"running": "yes", "adapter": "teamon", "detail": "x"}, "running is not a boolean"),
        ({"running": True, "adapter": "teamon"}, "no detail"),
        ({"running": True, "adapter": 3, "detail": "x"}, "the adapter is not a string"),
        ({"running": True, "adapter": "teamon", "detail": "x", "pid": -1}, "a negative pid"),
        (
            {"running": True, "adapter": "teamon", "detail": "x", "notes": "one"},
            "notes is a string",
        ),
    ],
)
def test_a_malformed_record_reads_as_absent_rather_than_half_understood(
    tmp_path: Path, payload: dict[str, object], why: str
) -> None:
    path = tmp_path / VOICE_RECORD_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_voice_record(path) is None, why
    with pytest.raises(VoiceRecordError):
        VoiceRecord.from_dict(payload)


def test_a_missing_record_is_absent_not_stopped(tmp_path: Path) -> None:
    assert read_voice_record(tmp_path / VOICE_RECORD_NAME) is None


def test_a_record_that_cannot_be_written_is_noted_and_not_raised(tmp_path: Path) -> None:
    """Losing the status file is not a reason to stop listening for a stop."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    record = publish_voice_record(blocked / VOICE_RECORD_NAME, a_record())

    assert any("could not be written" in note for note in record.notes)


# ---------------------------------------------------------------------------
# what status collects
# ---------------------------------------------------------------------------


def test_status_reports_the_configuration_when_no_companion_has_run(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(
        f'[voice]\nenabled = true\nadapter = "{ADAPTER_TEAMON}"\n', encoding="utf-8"
    )

    state = collect_voice_status(world.ctx, workspace)

    assert state.enabled is True
    assert state.adapter == ADAPTER_TEAMON
    assert state.record is None
    assert "voice run" in state.detail


def test_status_says_nothing_is_in_force_when_the_configuration_does_not_validate(
    tmp_path: Path,
) -> None:
    """The same document ``voice run`` would refuse to start on, read the same way."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text("[voice]\nenabled = 'yes'\n", encoding="utf-8")

    state = collect_voice_status(world.ctx, workspace)

    assert state.enabled is False
    assert "did not validate" in state.detail


def test_status_carries_the_record_of_a_companion_that_ran(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text("[voice]\nenabled = true\n", encoding="utf-8")
    publish_voice_record(
        workspace.state_dir / VOICE_RECORD_NAME, a_record(running=False, detail="it stopped")
    )

    state = collect_voice_status(world.ctx, workspace)

    assert state.record is not None
    assert state.record.running is False
    assert state.record.detail == "it stopped"


# ---------------------------------------------------------------------------
# a companion that ends badly
# ---------------------------------------------------------------------------


class BrokenClient(FakeTeamONClient):
    """A transport whose recogniser never opens, the way a held microphone is."""

    async def transcripts(self) -> AsyncIterator[TeamONTranscript]:
        raise RuntimeError("the recogniser never opened")


def test_a_companion_that_fails_says_so_and_leaves_no_record_claiming_to_listen(
    tmp_path: Path,
) -> None:
    """A record left saying "listening" by a process that is not is the worst state.

    It is the one ``status`` would report confidently and wrongly, and it is the
    reason the record is corrected on every exit rather than only on the clean
    one.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(
        f'[voice]\nenabled = true\nadapter = "{ADAPTER_TEAMON}"\n', encoding="utf-8"
    )
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = KEY  # type: ignore[index]

    code = run_voice_run(world.ctx, as_json=False, client=BrokenClient())

    assert code == EXIT_FAILURE
    assert "the recogniser never opened" in world.stderr
    record = read_voice_record(workspace.state_dir / VOICE_RECORD_NAME)
    assert record is not None
    assert record.running is False


# ---------------------------------------------------------------------------
# the command's exit codes
# ---------------------------------------------------------------------------


def test_check_exits_zero_for_a_phrase_that_resolves(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("voice", "check", "хватит") == EXIT_OK


def test_check_exits_non_zero_for_one_that_does_not(tmp_path: Path) -> None:
    """So a script can ask "does this phrase work" and get an answer it can test."""
    world = make_world(tmp_path)

    assert world.run("voice", "check", "бармаглот") == EXIT_FAILURE
