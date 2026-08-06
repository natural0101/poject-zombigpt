"""The voice seam, assembled the way ``pz-agent voice run`` assembles it.

``pz_agent_voice`` was complete and green and nothing imported it. Neither
console script reached it, no command started it, and no test could see that:
the package's own suite drives a companion the fixtures construct, and every
assertion in it passes whether or not a user can ever reach one.

The first test here is the one line that catches it — ``pz-agent voice check
стоп`` resolving to the stop intent, which today reaches the parser and yesterday
reached nothing at all. The one that *matters* is
:func:`test_a_spoken_stop_reaches_the_same_stop_the_cli_disarm_reaches`: a real
companion over a real ``SidecarLoop`` and a fake mod, with the assertion on the
loop's arming state rather than on any call being made, because a port that was
called and changed nothing is the failure this seam exists to rule out.

The stop route is the mod's own panic latch, and both consumers of it are driven
here: :class:`FakeMod` applies and clears the file exactly as
``PZAgent.Runtime.tick`` does, and the sidecar disarms on the tick that sees it.
That is why the effect is comparable with ``pz-agent disarm`` at all — the two
commands reach the same disarmed session by different routes, and only one of
them also reaches the game.

What this file cannot test is a live TeamON session. The SDK is not installed
here and its surface has never been verified from this repository, so
``voice run`` refuses on every real configuration — and that refusal is asserted
too, because a fallback to the fake adapter would look identical to success right
up until the user said «стоп» to a test double.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli import app
from pz_agent_cli.config import (
    ADAPTER_NONE,
    ADAPTER_TEAMON,
    SUPPORTED_VOICE_ADAPTERS,
    AgentConfig,
    default_config,
    load_config,
    validate_document,
)
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, Workspace, resolve_workspace
from pz_agent_cli.runtime import LoopLimits, SidecarLoop
from pz_agent_cli.voice import (
    VOICE_RECORD_NAME,
    GoalUnroutable,
    VoiceRecord,
    VoiceRefused,
    build_companion,
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
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_voice import phrases
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.driver import VoiceCompanion
from pz_agent_voice.messages import VoiceIntent
from pz_agent_voice.ports import VoiceServices
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.voice_doubles import FakeTeamONClient, settle

BUILD: Final = "42.20"

#: One tick per call, so every assertion below names the tick it is about.
LIMITS: Final = LoopLimits(
    tick_interval_ms=0,
    tick_budget=1,
    max_actions_per_window=2,
    action_window_ms=600_000,
    observations_per_tick=16,
    observation_window=8,
)

#: A key that is a key in shape only. It is never sent anywhere in this build —
#: no TeamON client exists to send it — and it is here to get past the credential
#: gate so the *next* refusal is the one under test.
FAKE_KEY: Final = "teamon-key-for-tests"


# ---------------------------------------------------------------------------
# the game side
# ---------------------------------------------------------------------------


@dataclass
class FakeMod:
    """The half of the exchange directory the mod owns.

    :meth:`tick` is ``PZAgent.Runtime.tick`` reduced to the part this file is
    about: read the panic latch as a level, stop what is queued, disarm, and
    clear the file afterwards. The clearing is not decoration — it is what makes
    the latch a request the game consumes rather than a permanent state, and a
    test that never cleared it would not notice a stop route that leaves the
    session unable to re-arm.
    """

    layout: IpcLayout
    monitor: HeartbeatMonitor
    clock: Any
    session_id: str = ""
    armed: bool = False
    mode: SessionMode = SessionMode.OBSERVE
    #: Mod-owned queue entries, which a panic stop clears and nothing else does.
    queued: list[str] = field(default_factory=lambda: ["consume.eat"])
    stops: int = 0
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)

    def beat(self) -> None:
        self.monitor.publish(
            Peer.GAME,
            session_id=self.session_id,
            nonce=self.nonce,
            version=PRODUCT_VERSION,
            build=BUILD,
            player_present=True,
            armed=self.armed,
            mode=self.mode,
            danger_level=DangerLevel.NONE,
        )

    def latch_requested(self) -> bool:
        """True when the panic file holds anything at all, which is what the mod reads."""
        latch = self.layout.panic_stop
        return latch.is_file() and bool(latch.read_text(encoding="utf-8").strip())

    def tick(self) -> bool:
        """One mod tick. Returns whether this one stopped anything."""
        if not self.latch_requested():
            self.beat()
            return False
        self.stops += 1
        self.queued.clear()
        self.armed = False
        self.mode = SessionMode.OBSERVE
        self.layout.panic_stop.write_text("", encoding="utf-8")
        self.beat()
        return True


# ---------------------------------------------------------------------------
# the assembly under test
# ---------------------------------------------------------------------------


@dataclass
class Listening:
    """A companion running over the real ports, the real loop and the fake mod."""

    world: CliWorld
    workspace: Workspace
    loop: SidecarLoop
    mod: FakeMod
    services: VoiceServices
    adapter: FakeVoiceAdapter
    companion: VoiceCompanion
    task: asyncio.Task[None] | None = None

    @property
    def latch(self) -> Path:
        return self.mod.layout.panic_stop

    def arm(self, mode: SessionMode = SessionMode.ASSISTED) -> None:
        self.mod.beat()
        outcome = self.loop.arm(mode)
        assert outcome.armed, outcome.detail
        self.mod.armed = True
        self.mod.mode = mode
        self.mod.beat()

    def session_state(self) -> tuple[bool, SessionMode]:
        """What a user would call "is it stopped": armed, and in which mode."""
        return self.loop.armed, self.loop.mode

    async def start(self) -> None:
        self.task = asyncio.create_task(self.companion.run())
        await settle()

    async def say(self, phrase: str, *, final: bool = True, confidence: float = 1.0) -> None:
        """Push one transcript through the adapter's stream and let it be handled."""
        self.adapter.push(phrase, final=final, confidence=confidence)
        await settle()

    async def finish(self) -> None:
        self.adapter.close()
        task = self.task
        if task is None:
            return
        await settle()
        assert task.done(), "the companion did not shut down after its stream closed"
        await task

    def run(self, *argv: str) -> int:
        """Run a real CLI command against the same machine, streams reset."""
        self.world.reset_streams()
        return self.world.run(*argv)

    def __enter__(self) -> Listening:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.loop.session is not None:
            self.loop.shutdown(reason="the test finished")


def write_config(
    world: CliWorld,
    *,
    enabled: bool = True,
    adapter: str = ADAPTER_TEAMON,
    key_env: str = DEFAULT_TEAMON_KEY_ENV,
) -> Path:
    """Write the ``[voice]`` section a user would write, and return its path."""
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(
        "[voice]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        f'adapter = "{adapter}"\n'
        f'api_key_env = "{key_env}"\n',
        encoding="utf-8",
    )
    return workspace.config_path


def assemble(tmp_path: Path, **config: Any) -> Listening:
    """Build the loop, the mod and the companion over one exchange directory.

    The ports come from :func:`~pz_agent_cli.voice.voice_services` and the
    companion from :func:`~pz_agent_cli.voice.build_companion` — the same two
    calls ``voice run`` makes — because a test that assembled the session its own
    way would prove something about the test rather than about the command. Only
    the adapter is substituted, and it has to be: the shipped one needs a vendor
    SDK that is not installed, which is itself asserted below.
    """
    world = make_world(tmp_path)
    write_config(world, **config)
    world.clock.freeze()
    workspace = resolve_workspace(world.ctx)
    assert workspace.ipc_root is not None
    layout = IpcLayout(workspace.ipc_root)
    layout.ensure()
    monitor = HeartbeatMonitor(layout, clock=world.ctx.clock_ms)

    mod = FakeMod(layout=layout, monitor=monitor, clock=world.clock)
    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)
    attach = loop.attach()
    assert attach.attached, attach.detail
    session = loop.session
    assert session is not None
    mod.session_id = session.session_id
    mod.beat()
    # The pid record is what ``pz-agent disarm`` looks for before it publishes a
    # control request; a foreground sidecar claims it, and so does this.
    app.build_supervisor(world.ctx, workspace).pid_file.claim(os.getpid())

    services = voice_services(workspace, clock=world.ctx.clock_ms)
    adapter = FakeVoiceAdapter(clock=world.ctx.clock_ms)
    return Listening(
        world=world,
        workspace=workspace,
        loop=loop,
        mod=mod,
        services=services,
        adapter=adapter,
        companion=build_companion(adapter, services, clock=world.ctx.clock_ms),
    )


# ---------------------------------------------------------------------------
# the phrase reaches the parser at all
# ---------------------------------------------------------------------------


def test_voice_check_resolves_the_stop_word(tmp_path: Path) -> None:
    """The assertion that would have caught the original defect.

    Nothing in this build could reach :func:`~pz_agent_voice.intent.classify`
    from a command line — no entry point led to the package — so the matcher's
    own green suite said nothing about whether a user could ever consult it.
    """
    world = make_world(tmp_path)

    code = world.run("voice", "check", "стоп")

    assert code == EXIT_OK
    assert "stop" in world.stdout
    assert "стоп" in world.stdout


def test_voice_check_answers_in_json_for_a_script(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    code = world.run("voice", "check", "агент", "поешь", "--json")

    assert code == EXIT_OK
    assert '"intent": "goal"' in world.stdout
    assert '"goal": "eat"' in world.stdout
    assert '"woke": true' in world.stdout


def test_voice_check_says_plainly_when_nothing_matched(tmp_path: Path) -> None:
    """ "Why was «стоп» not recognised" is answerable without a session."""
    world = make_world(tmp_path)

    code = world.run("voice", "check", "стопка")

    assert code == EXIT_FAILURE
    assert "nothing matched" in world.stdout
    # The token is printed, because it is the answer: the matcher compares whole
    # words, and seeing the word is what tells the user why theirs is not one.
    assert "стопка" in world.stdout


def test_voice_check_reads_a_phrase_through_the_session_matcher(tmp_path: Path) -> None:
    """The command answers from :func:`classify`, not from a copy of its table."""
    reading = read_phrase("агент, остановись")

    assert reading.intent is VoiceIntent.STOP
    assert reading.recognised is True


def test_voice_check_needs_no_game_no_config_and_no_session(tmp_path: Path) -> None:
    """A machine with nothing installed still answers what a phrase would do."""
    world = make_world(tmp_path, with_game=False, with_user_dir=False)

    assert world.run("voice", "check", "стоп") == EXIT_OK


def test_voice_without_a_subcommand_says_which_ones_exist(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    code = world.run("voice")

    assert code == EXIT_FAILURE
    assert "run, check" in world.stderr


# ---------------------------------------------------------------------------
# the spoken stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spoken_stop_reaches_the_same_stop_the_cli_disarm_reaches(
    tmp_path: Path,
) -> None:
    """The effect, not the call: the loop ends up in the state ``disarm`` leaves it in.

    Both routes are driven against the same assembled loop, one after the other,
    so the comparison is between two observed session states rather than between
    a state and a remembered expectation.
    """
    with assemble(tmp_path) as live:
        live.arm()
        assert live.session_state() == (True, SessionMode.ASSISTED)

        assert live.run("disarm") == EXIT_OK
        live.loop.tick()
        by_the_cli = live.session_state()

        live.arm()
        await live.start()
        await live.say("стоп")

        assert live.mod.latch_requested(), "the stop never reached the mod's panic latch"
        live.loop.tick()
        by_voice = live.session_state()
        await live.finish()

    assert by_the_cli == (False, SessionMode.OBSERVE)
    assert by_voice == by_the_cli


@pytest.mark.asyncio
async def test_a_spoken_stop_reaches_the_mod_itself_which_the_cli_disarm_does_not(
    tmp_path: Path,
) -> None:
    """The route is the panic latch, so the game stops too — and clears the file.

    ``disarm`` takes the sidecar's authority away and leaves whatever the mod is
    already running to finish. The stop word is the blueprint's shortest path and
    has to reach further than that, which is the whole reason it does not travel
    through the control channel.
    """
    with assemble(tmp_path) as live:
        live.arm()
        await live.start()

        await live.say("стоп")
        stopped = live.mod.tick()

        await live.finish()

    assert stopped is True
    assert live.mod.stops == 1
    assert live.mod.queued == [], "the mod-owned queue survived a panic stop"
    assert live.mod.armed is False
    assert not live.mod.latch_requested(), "the mod did not clear the latch it consumed"


@pytest.mark.asyncio
async def test_a_stop_decided_on_an_interim_transcript_does_not_wait_for_the_final_one(
    tmp_path: Path,
) -> None:
    """No final transcript is ever pushed, and the latch is down all the same.

    This is the latency requirement stated as a fact rather than as a number: the
    only transcript this session ever sees is a low-confidence guess, and it is
    enough. A companion that waited for the recogniser to endpoint would leave
    the latch absent here.
    """
    with assemble(tmp_path) as live:
        live.arm()
        await live.start()

        await live.say("сто", final=False, confidence=0.2)
        assert not live.latch.exists(), "a partial word that is not a stop word stopped the agent"

        await live.say("стоп", final=False, confidence=0.2)

        assert live.mod.latch_requested(), "an interim stop did not reach the latch"
        live.loop.tick()
        state = live.session_state()
        await live.finish()

    assert state == (False, SessionMode.OBSERVE)


@pytest.mark.asyncio
async def test_a_phrase_that_matches_nothing_is_reported_and_causes_no_action(
    tmp_path: Path,
) -> None:
    """An agent that guesses at a misheard command is worse than one that asks."""
    with assemble(tmp_path) as live:
        live.arm()
        await live.start()

        # Woken first, so what is under test is a listening companion hearing a
        # word it does not know — not a phrase discarded for want of a wake word,
        # which is a different silence with a different cause.
        await live.say("агент")
        await live.say("бармаглот")

        turn = live.companion.last_turn
        assert turn is not None
        assert turn.intent is VoiceIntent.UNKNOWN
        assert [message.text for message in live.adapter.started] == [phrases.NOT_UNDERSTOOD]
        assert turn.plan is None
        assert not live.latch.exists(), "an unrecognised phrase stopped the agent"
        live.loop.tick()
        state = live.session_state()
        await live.finish()

    assert state == (True, SessionMode.ASSISTED), "an unrecognised phrase changed the session"
    assert live.mod.queued == ["consume.eat"], "an unrecognised phrase touched the mod's queue"


@pytest.mark.asyncio
async def test_a_spoken_goal_is_refused_and_submitted_nowhere(tmp_path: Path) -> None:
    """The gap this build has, asserted as a gap rather than left to be discovered.

    There is no channel from a second process into the running sidecar's planner,
    so the goal is refused and nothing is written anywhere. What must not happen
    is a goal that quietly reaches the command queue: that would put the
    microphone past the reflex guard and the capability gate in one step.
    """
    with assemble(tmp_path) as live:
        live.arm()
        queue_before = live.mod.layout.command_queue.read_bytes() if _exists(live) else b""
        await live.start()

        await live.say("агент, поешь")

        turn = live.companion.last_turn
        assert turn is not None
        assert turn.plan is None
        assert "GoalUnroutable" in turn.detail
        queue_after = live.mod.layout.command_queue.read_bytes() if _exists(live) else b""
        await live.finish()

    assert queue_after == queue_before, "a spoken goal reached the command queue"
    with pytest.raises(GoalUnroutable):
        live.services.plans.current()


def _exists(live: Listening) -> bool:
    return live.mod.layout.command_queue.is_file()


@pytest.mark.asyncio
async def test_a_stop_that_cannot_be_latched_is_reported_as_a_failure(tmp_path: Path) -> None:
    """The one thing worse than not stopping is saying you did.

    The exchange directory is replaced by a file, so the write fails the way a
    read-only or vanished profile directory would. The companion must say the
    stop failed rather than acknowledge one.
    """
    with assemble(tmp_path) as live:
        live.arm()
        await live.start()
        _make_unwritable(live.mod.layout)

        await live.say("стоп")

        turn = live.companion.last_turn
        assert turn is not None
        assert turn.intent is VoiceIntent.STOP
        assert turn.stop is None, "a stop with no latch reported a stop report"
        spoken = [message.text for message in live.adapter.started]
        await live.finish()

    assert spoken and spoken[-1] == phrases.STOP_FAILED


def _make_unwritable(layout: IpcLayout) -> None:
    """Leave a directory where the latch has to go, so the write cannot succeed."""
    layout.panic_stop.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# which adapter, and the one that must never be selected
# ---------------------------------------------------------------------------


def test_teamon_selected_but_unconstructible_refuses_and_does_not_fall_back(
    tmp_path: Path,
) -> None:
    """A configured adapter that cannot be built stops the command, not the honesty.

    The SDK is not installed in this environment and its surface has never been
    verified from this repository, so this is the ordinary outcome of ``voice
    run`` on every machine today. What matters is which of the several honest
    answers it gives: a refusal naming the missing step, and not a companion
    running on something else.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]

    # Asserted on the selector as well as on the command, and not only for
    # thoroughness: an adapter that falls back to the fake makes ``voice run``
    # block on a stream that never ends, and a test that hangs reports nothing.
    with pytest.raises(VoiceRefused):
        select_adapter(
            load_config(resolve_workspace(world.ctx).config_path).config or default_config(),
            env=world.ctx.env,
            clock=world.ctx.clock_ms,
        )

    code = world.run("voice", "run")

    assert code == EXIT_FAILURE
    assert "teamon" in world.stderr.lower()
    assert "fake" not in world.stderr.lower(), "the refusal offered a test double"
    assert "Nothing was started" in world.stderr
    workspace = resolve_workspace(world.ctx)
    assert read_voice_record(workspace.state_dir / VOICE_RECORD_NAME) is None


def test_a_missing_credential_is_refused_by_name_and_never_read_from_the_file(
    tmp_path: Path,
) -> None:
    """The key lives in the environment; the file names only the variable."""
    world = make_world(tmp_path)
    write_config(world, key_env="PZ_AGENT_VOICE_KEY_FOR_TEST")

    code = world.run("voice", "run")

    assert code == EXIT_FAILURE
    assert "PZ_AGENT_VOICE_KEY_FOR_TEST" in world.stderr
    assert FAKE_KEY not in world.stderr


def test_a_key_written_into_the_file_is_refused_by_the_validator(tmp_path: Path) -> None:
    """``api_key_env`` names a variable, and a pasted key does not look like one."""
    validation = validate_document(
        {"voice": {"enabled": True, "adapter": ADAPTER_TEAMON, "api_key_env": "sk-live-abc123"}},
        path=tmp_path / "config.toml",
    )

    assert not validation.ok
    assert any(problem.path == "voice.api_key_env" for problem in validation.errors)


def test_voice_disabled_refuses_before_it_builds_anything(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world, enabled=False)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]

    code = world.run("voice", "run")

    assert code == EXIT_FAILURE
    assert "voice.enabled" in world.stderr


def test_adapter_none_refuses_rather_than_pretending_to_listen(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world, adapter=ADAPTER_NONE)

    code = world.run("voice", "run")

    assert code == EXIT_FAILURE
    assert "nothing to listen with" in world.stderr


def test_the_fake_adapter_is_not_selectable_in_a_shipped_configuration(tmp_path: Path) -> None:
    """Three ways it must not be reachable, because one of them alone is a lock on a door.

    A configuration cannot name it, the validator refuses the word, and the
    selector refuses it even when handed a configuration no validator would have
    produced. The last one is the one that matters: a fake adapter answers
    scripted transcripts, so a user would be told the companion is listening and
    would say «стоп» to a list.
    """
    assert "fake" not in SUPPORTED_VOICE_ADAPTERS

    validation = validate_document(
        {"voice": {"enabled": True, "adapter": "fake"}}, path=tmp_path / "config.toml"
    )
    assert not validation.ok

    hand_built = AgentConfig(
        values={
            **default_config().values,
            "voice": {"enabled": True, "adapter": "fake", "api_key_env": DEFAULT_TEAMON_KEY_ENV},
        }
    )
    with pytest.raises(VoiceRefused, match="not an adapter this build constructs"):
        select_adapter(
            hand_built,
            env={DEFAULT_TEAMON_KEY_ENV: FAKE_KEY},
            clock=lambda: 0,
        )


def test_a_supplied_teamon_client_is_what_gets_constructed(tmp_path: Path) -> None:
    """The integrator's seam builds the TeamON adapter and nothing else.

    ``TeamONClient`` is the three-method contract the package documents in place
    of guessing at the vendor's SDK. Passing one is the only way an adapter is
    built in this build, and this is the assertion that it builds *that* adapter
    rather than falling back to anything.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)

    code = run_voice_run(world.ctx, as_json=False, client=FakeTeamONClient())

    assert code == EXIT_OK
    record = read_voice_record(workspace.state_dir / VOICE_RECORD_NAME)
    assert record is not None
    assert record.adapter == ADAPTER_TEAMON
    assert record.running is False, "the record still says listening after the stream ended"
    assert record.goals_routed is False


# ---------------------------------------------------------------------------
# what status says
# ---------------------------------------------------------------------------


def test_status_shows_the_configured_adapter(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world)

    assert world.run("status") == EXIT_OK

    assert "voice" in world.stdout
    assert ADAPTER_TEAMON in world.stdout
    assert "nothing listening" in world.stdout


def test_status_shows_voice_switched_off_as_off(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world, enabled=False)

    assert world.run("status") == EXIT_OK

    assert "voice.enabled is false" in world.stdout


def test_status_says_a_companion_is_listening_and_what_it_cannot_do(tmp_path: Path) -> None:
    """The record is written by the process that holds the adapter, and read here."""
    world = make_world(tmp_path)
    write_config(world)
    workspace = resolve_workspace(world.ctx)
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    publish_voice_record(
        workspace.state_dir / VOICE_RECORD_NAME,
        VoiceRecord(
            running=True,
            adapter=ADAPTER_TEAMON,
            detail="a companion is listening on teamon",
            pid=4242,
            started_at_ms=1_700_000_000_000,
        ),
    )

    assert world.run("status") == EXIT_OK

    assert "LISTENING" in world.stdout
    assert "pid 4242" in world.stdout
    assert "a spoken goal reaches no planner" in world.stdout


def test_status_reports_voice_in_json_for_a_bug_report(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world)

    assert world.run("status", "--json") == EXIT_OK

    assert '"voice"' in world.stdout
    assert f'"adapter": "{ADAPTER_TEAMON}"' in world.stdout
