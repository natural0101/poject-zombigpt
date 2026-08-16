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

The goal channel is the subject of the rest, and it is checked the same way. The
placeholder this file used to test — a plan port that refused every submission —
is gone, so "the goal went somewhere" can no longer be asserted by watching an
exception arrive. It is asserted against a real
:class:`~pz_agent_core.rpc.transport.RpcServer` on a real socket, with a real
descriptor and a real token, because every cheaper double answers the same way
whether or not a wire exists. The test that matters most is the one that starts
that server, watches the probe report the channel reachable, closes the server
*without* removing its descriptor, and watches the same probe say the channel is
gone: a check that read the descriptor and stopped there would report a routed
channel with nothing listening, which is the exact defect this milestone removed.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest

from pz_agent_cli.config import ADAPTER_NONE, ADAPTER_TEAMON, AgentConfig, default_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from pz_agent_cli.supervisor import CONTROL_FILE_NAME, SidecarSupervisor
from pz_agent_cli.voice import (
    VOICE_RECORD_NAME,
    ExchangeSessionPort,
    GoalChannelReading,
    VoiceRecord,
    VoiceRecordError,
    VoiceRefused,
    _log_safely,
    adapter_name,
    collect_voice_status,
    probe_goal_channel,
    publish_voice_record,
    read_phrase,
    read_voice_record,
    run_voice_run,
    select_adapter,
    voice_services,
)
from pz_agent_core.diagnostics import DiagnosticLog, LogLevel
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.planner.providers import DEFAULT_TEAMON_KEY_ENV
from pz_agent_core.protocol import DangerLevel, JsonDict, SessionMode
from pz_agent_core.rpc.descriptor import runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from pz_agent_core.session.heartbeat import DEFAULT_TIMEOUT_MS, HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.adapters.teamon import TeamONTranscript, TeamONVoiceAdapter
from pz_agent_voice.messages import MAX_TRANSCRIPT_CHARS, VoiceGoal, VoiceIntent
from pz_agent_voice.ports import PlanRequest, SidecarUnavailable
from tests.fixtures.cli_worlds import make_world
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.voice_doubles import FakeTeamONClient

BUILD: Final = "42.20"

KEY: Final = "teamon-key-for-tests"

#: Long enough that a loaded runner does not fail a happy path, short enough
#: that a genuine hang ends one test rather than the suite's patience.
GRACE: Final = 10.0

#: What a core answers to each method the voice path may call, written out by
#: hand rather than produced by the encoders under test: a body built from
#: ``encode_goal_channel_status`` would assert that the codec equals itself, and
#: a key misspelled in both halves would survive every round trip.
#:
#: ``plan.execute`` is the method this build's spoken goal takes and
#: ``goal.submit`` is the typed channel it is moving to (``docs/control/
#: DECISIONS.md``); both are answered so that the assertions below are about the
#: request *arriving at a core*, which is the claim, rather than about which of
#: the two carried it.
CORE_ANSWERS: Final[dict[str, JsonDict]] = {
    "goal.status": {"active": None, "pending": [], "named": None},
    "plan.execute": {
        "plan_id": "plan-9",
        "status": "started",
        "step_index": 2,
        "steps": [
            {
                "index": 0,
                "action": "movement.move_to",
                "status": "succeeded",
                "reason_code": "POSTCONDITION_MET",
                "action_id": "action-4",
            },
            {"index": 1, "action": "consume.eat", "status": "started"},
        ],
    },
}


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


def test_a_latch_that_lands_empty_is_not_reported_as_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that reports success and leaves nothing is still not a stop.

    The mod reads the latch as "any non-empty content stops, empty does not", so
    an empty file is indistinguishable from no file at all. Every *ordinary* way
    the write fails already raises — a directory in its place, a read-only
    exchange, a full disk — and the test above covers that. This check is for
    the case those cannot produce: the write returned and the file is empty
    anyway. Deleting it left the full suite green (9471 passed), and the
    companion would answer «Остановился.» while the agent kept acting, which is
    the fabricated success AGENTS.md's honest-state rule exists to forbid.

    The write is made to report success and land nothing, because that is the
    one failure this line is about; nothing else on the stop path is disturbed.
    """
    clock = FakeClock()
    port = a_port(tmp_path, clock=clock)
    real_write_text = Path.write_text

    def write_that_lands_nothing(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == port.layout.panic_stop.name:
            self.touch()
            return 0
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", write_that_lands_nothing)

    with pytest.raises(OSError, match="stops nothing"):
        port.stop()


def test_logging_that_fails_mid_session_is_not_why_the_companion_stops(
    tmp_path: Path,
) -> None:
    """The handler is live code, and nothing else covers what it covers.

    ``_companion_log`` catches ``OSError`` when the log is *built*, so a logs
    directory that is unwritable at start is handled. This one is for the tenth
    record: a directory that fills or becomes unwritable mid-session. The
    diagnostic log writes through a real rotating file and lets ``OSError`` out,
    and both call sites that matter run outside ``_serve``'s only ``try`` — so
    without the handler the user gets a traceback instead of the ending
    sentence, from a failure that has nothing to do with the agent.

    Deleting it left the suite green (9471 passed): nothing made a log raise.
    """

    class FailingLog:
        def log(self, *args: object, **kwargs: object) -> None:
            raise OSError("the logs directory filled up")

    _log_safely(cast(DiagnosticLog, FailingLog()), LogLevel.INFO, "voice.test", detail="x")


def test_the_line_voice_run_prints_goes_through_the_redactor(tmp_path: Path) -> None:
    """Everything this command prints is pasted into bug reports, this line too.

    On the failure branch the ending sentence embeds the backend exception
    verbatim, and an ``OSError`` or an SDK error routinely carries an absolute
    path. The redactor is built from exactly those tokens — the Zomboid user
    directory, the home and install directories, the account name — so dropping
    the one call on the printed message is what puts them on the terminal and
    in the ``--json`` payload.

    The record and the log keep their own redaction calls and the support
    bundle redacts every member, so the archive stayed covered; stdout is what
    nothing else protects. ``test_voice_privacy.py`` did not catch it because it
    scans for a *transcript* canary, not for paths.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(
        f'[voice]\nenabled = true\nadapter = "{ADAPTER_TEAMON}"\n', encoding="utf-8"
    )
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = KEY  # type: ignore[index]
    secret = str(workspace.user_dir)
    assert secret, "the world has no user directory, so this test would prove nothing"

    class LeakingClient(FakeTeamONClient):
        """A backend whose failure carries a path, which is the ordinary case."""

        async def transcripts(self) -> AsyncIterator[TeamONTranscript]:
            raise RuntimeError(f"cannot read {secret}/Lua/pz_agent: permission denied")

    code = run_voice_run(world.ctx, as_json=False, client=LeakingClient())

    assert code == EXIT_FAILURE
    printed = world.stdout + world.stderr
    assert "permission denied" in printed, "the failure never reached the terminal"
    assert secret not in printed, (
        "voice run printed the user directory verbatim; every line this command "
        "prints goes through the redactor for the reason this one just showed"
    )


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
# the goal route, against a core that is really there and really gone
# ---------------------------------------------------------------------------


@dataclass
class Core:
    """A real RPC server on a real socket, and what it was asked."""

    state_dir: Path
    server: RpcServer
    thread: threading.Thread
    seen: list[RpcRequest]
    #: What this core answers, live: the dispatcher reads it on every request,
    #: so a test may take a method away from a *running* core. That is the one
    #: way to reach the "something answered and it was not an answer" branch
    #: without a second fixture, and it is a real state — a sidecar from another
    #: build accepts the connection and refuses the method.
    answers: dict[str, JsonDict]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.seen]

    def close(self) -> None:
        """Stop answering, and deliberately leave the descriptor behind.

        A sidecar killed rather than stopped leaves exactly this state, and it is
        the state that separates a probe which dials from one which reads a file
        and calls that routed.
        """
        self.server.close()
        self.thread.join(timeout=GRACE)


Start = Callable[[Path], Core]


@pytest.fixture
def start_core() -> Iterator[Start]:
    """Bring up real cores and take them all down again.

    State directory names are given by the caller and kept short on purpose: a
    POSIX socket path is bounded by ``sun_path``, and the profile directory a
    :func:`~tests.fixtures.cli_worlds.make_world` machine hands out is far too
    deep to bind under.
    """
    started: list[Core] = []

    def _start(state_dir: Path) -> Core:
        runtime = runtime_dir(state_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        seen: list[RpcRequest] = []
        answers = dict(CORE_ANSWERS)

        def dispatch(request: RpcRequest) -> RpcResponse:
            seen.append(request)
            answer = answers.get(request.method)
            if answer is None:
                return RpcResponse(
                    id=request.id,
                    ok=False,
                    error_code="UNKNOWN_METHOD",
                    error_message=f"this fixture does not answer {request.method}",
                )
            return RpcResponse(id=request.id, ok=True, result=dict(answer))

        server = RpcServer(new_address(runtime), authkey=key, handler=dispatch)
        write_descriptor(state_dir, server.descriptor())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        core = Core(state_dir=state_dir, server=server, thread=thread, seen=seen, answers=answers)
        started.append(core)
        return core

    yield _start

    for core in started:
        core.close()


def test_a_goal_channel_nobody_is_listening_on_is_reported_unreachable(tmp_path: Path) -> None:
    """The state every machine is in until ``pz-agent start`` is run.

    Naming the command is the point: "unreachable" on its own sends a user
    looking for a broken install.
    """
    reading = probe_goal_channel(tmp_path / "s")

    assert reading.reachable is False
    assert "pz-agent start" in reading.detail


def test_a_running_core_answers_the_probe_and_the_probe_says_it_was_asked(
    tmp_path: Path, start_core: Start
) -> None:
    """Reachable means a request was made and answered, not that a file exists."""
    core = start_core(tmp_path / "s")

    reading = probe_goal_channel(core.state_dir)

    assert reading.reachable is True
    # Written out rather than taken from ``Method``: the two halves of the link
    # have to agree on the string, and deriving it here would agree with itself.
    assert core.methods == ["goal.status"]
    # A read, and only a read. A probe that submitted something to find out
    # whether it could submit would be a spoken goal nobody said.
    assert "goal.submit" not in core.methods


def test_a_core_that_stopped_answering_stops_being_reported_as_routed(
    tmp_path: Path, start_core: Start
) -> None:
    """The load-bearing one, and the reason the probe dials at all.

    The descriptor is still on disk after the server closes, and it still names
    a live process — this one. Every check short of a connection therefore
    passes, and every one of them would report a routed channel into a socket
    nothing is accepting on.
    """
    core = start_core(tmp_path / "s")
    assert probe_goal_channel(core.state_dir).reachable is True

    core.close()
    after = probe_goal_channel(core.state_dir)

    assert (core.state_dir / "runtime" / "core-rpc.json").is_file(), "the descriptor was removed"
    assert after.reachable is False
    assert "pz-agent start" in after.detail


def test_a_core_that_answers_but_refuses_the_probe_is_not_reported_as_routed(
    tmp_path: Path, start_core: Start
) -> None:
    """Something is there, and this build cannot use it, which is not routed either.

    A sidecar from another build accepts the connection, authenticates it and
    then does not have the method — so every reading that stops at "a socket
    accepted me" reports the channel up. The reading has to be the *answer*, and
    the answer here is a refusal, which is reported with the remedy for the same
    reason the absent case is: the user's next step is the same command.
    """
    core = start_core(tmp_path / "s")
    del core.answers["goal.status"]

    reading = probe_goal_channel(core.state_dir)

    assert core.methods == ["goal.status"], "the probe decided without asking"
    assert reading.reachable is False
    assert "pz-agent start" in reading.detail


def test_the_services_the_command_builds_reach_a_real_core(
    tmp_path: Path, start_core: Start
) -> None:
    """The whole of the wiring: a goal submitted through what ``voice run`` builds.

    The assertion is on what the *server* received. A bundle that refused
    locally — which is what this build shipped — would leave ``seen`` empty
    however plausible its exception was.
    """
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
    workspace = resolve_workspace(world.ctx)
    core = start_core(workspace.state_dir)

    services = voice_services(workspace, clock=world.ctx.clock_ms)
    record = services.plans.execute(a_goal())

    assert core.methods == ["plan.execute"]
    assert core.seen[0].params["goal"] == "eat"
    assert record.plan_id == "plan-9"


def test_the_stop_stays_off_the_link_the_goal_takes(tmp_path: Path, start_core: Start) -> None:
    """Two transports, and the stop keeps the one that works with nothing running."""
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
    workspace = resolve_workspace(world.ctx)
    assert workspace.ipc_root is not None
    # The exchange directory is the mod's to create, and the stop writes into it.
    IpcLayout(workspace.ipc_root).ensure()
    core = start_core(workspace.state_dir)

    services = voice_services(workspace, clock=world.ctx.clock_ms)
    assert isinstance(services.session, ExchangeSessionPort)
    services.session.stop()

    assert core.methods == [], "the stop dialled the goal channel"
    assert services.session.layout.panic_stop.read_text(encoding="utf-8").strip()


def test_a_goal_with_no_core_is_refused_by_the_link_rather_than_by_this_process(
    tmp_path: Path,
) -> None:
    """Nothing here answers "not routed" any more; the link answers "not running".

    The difference is what the user does next. The first is a build without the
    feature and there is nothing to do about it; the second names the command
    that fixes it.
    """
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
    workspace = resolve_workspace(world.ctx)

    services = voice_services(workspace, clock=world.ctx.clock_ms)

    with pytest.raises(SidecarUnavailable, match="pz-agent start"):
        services.plans.execute(a_goal())


def a_goal(goal: str = VoiceGoal.EAT.value) -> PlanRequest:
    return PlanRequest(
        goal=goal,
        mode=SessionMode.ASSISTED,
        max_steps=4,
        max_real_seconds=60,
        idempotency_key="key-1",
    )


def test_no_source_file_in_this_package_refuses_a_goal_unrouted() -> None:
    """The placeholder is gone from the CLI and must not grow back.

    ``tests/unit/test_voice_plan_port.py`` makes the same scan over
    :mod:`pz_agent_voice`, and scoped it there deliberately — these names were
    this package's to remove. This is the other half, so neither half can grow
    the refusal back while the other's scan stays green.

    An empty ``offenders`` proves nothing on its own: a root that stopped
    resolving makes :meth:`Path.rglob` yield nothing and the assertion below pass
    without reading a line. So the scan is required to have found the module it
    is scanning *for* first.
    """
    root = Path(__file__).resolve().parents[2] / "packages" / "pz_agent_cli" / "src"
    scanned = {path: path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))}

    assert root / "pz_agent_cli" / "voice.py" in scanned, f"scanned nothing under {root}"

    offenders = [
        path.name
        for path, source in scanned.items()
        if "Unrouted" in source or "GoalUnroutable" in source
    ]

    assert offenders == []


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


def exploding_probe() -> GoalChannelReading:
    """A probe that fails the test if a phrase that is not a goal consults it."""
    raise AssertionError("a phrase that needs no sidecar dialled one")


@pytest.mark.parametrize("phrase", ["стоп", "бармаглот", "агент"])
def test_only_a_goal_asks_the_channel_anything(phrase: str) -> None:
    """A stop must be answerable on a machine with no sidecar and no socket."""
    reading = read_phrase(phrase, channel=exploding_probe)

    assert reading.channel is None


def test_a_goal_carries_the_channels_own_answer(tmp_path: Path) -> None:
    """The reading reports what the probe said, and does not summarise it away."""
    answered = GoalChannelReading(reachable=False, detail="nothing answered; run 'pz-agent start'")

    reading = read_phrase("агент, поешь", channel=lambda: answered)

    assert reading.goal is VoiceGoal.EAT
    assert reading.channel == answered
    assert reading.to_dict()["goal_channel"] == {
        "reachable": False,
        "detail": "nothing answered; run 'pz-agent start'",
    }


def test_a_phrase_read_without_a_probe_claims_nothing_about_the_channel() -> None:
    """Silence rather than a default: "routed" is the answer that must be earned."""
    assert read_phrase("агент, поешь").channel is None


def test_voice_check_reports_the_channel_as_unreachable_and_names_the_command(
    tmp_path: Path,
) -> None:
    """The command a user runs before they own a sidecar, answered honestly."""
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")

    code = world.run("voice", "check", "агент", "поешь")

    assert code == EXIT_OK, "the phrase resolves; it is the sidecar that is missing"
    assert "UNREACHABLE" in world.stdout
    assert "pz-agent start" in world.stdout


def test_voice_check_reports_a_live_channel_as_reachable(tmp_path: Path, start_core: Start) -> None:
    """And it is the same command, on the same machine, with a core added."""
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
    core = start_core(resolve_workspace(world.ctx).state_dir)

    code = world.run("voice", "check", "агент", "поешь", "--json")

    assert code == EXIT_OK
    assert core.methods == ["goal.status"]
    assert '"reachable": true' in world.stdout
    assert "UNREACHABLE" not in world.stdout


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


def test_a_record_that_never_said_where_goals_went_claims_no_route(tmp_path: Path) -> None:
    """An omission is not a claim, here as it is for a heartbeat that omits ``armed``.

    Both defaults are asserted against the literal ``False`` rather than against
    each other: a record this process built without naming the field, and a
    document on disk from a build that had no such field to name. If either read
    as routed, ``status`` would tell a user that a companion which never had a
    goal channel was submitting goals over one.
    """
    assert a_record().goals_routed is False

    path = tmp_path / VOICE_RECORD_NAME
    path.write_text(
        json.dumps({"running": True, "adapter": ADAPTER_TEAMON, "detail": "it listened"}),
        encoding="utf-8",
    )

    record = read_voice_record(path)

    assert record is not None
    assert record.goals_routed is False


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
