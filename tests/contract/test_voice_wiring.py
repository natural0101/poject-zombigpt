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

The goal route is the other one, and it is a socket
--------------------------------------------------

A spoken goal used to be refused here on the grounds that no channel carried one
into a running sidecar. It does now, so the refusal is gone and with it the only
way a test could show a goal "went somewhere" without a wire existing. What
replaces it is a real :class:`~pz_agent_core.rpc.transport.RpcServer` on a real
socket, with a real descriptor and a real token, in the same state directory
``pz-agent start`` would write them into — and the assertion is on what that
server *received*. A bundle that refused locally leaves it empty however
plausible the exception.

Which method carries the goal is deliberately not asserted. ``docs/control/
DECISIONS.md`` records the move from ``plan.execute`` to the typed goal channel,
and both are methods of the same link reached through the same wiring function;
what this file is responsible for is that the CLI's wiring reaches a core at all,
with the spoken goal named in the request. The full round trip — admission,
exclusivity, budget — is ``tests/contract/test_voice_goal_e2e.py``.

What this file cannot test is a live TeamON session. The SDK is not installed
here and its surface has never been verified from this repository, so
``voice run`` refuses on every real configuration — and that refusal is asserted
too, because a fallback to the fake adapter would look identical to success right
up until the user said «стоп» to a test double.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
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
from pz_agent_core.protocol import DangerLevel, JsonDict, SessionMode
from pz_agent_core.rpc.descriptor import runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_core.rpc.wire import ErrorCode, RpcRequest, RpcResponse
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_voice import phrases
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.adapters.teamon import TeamONTranscript
from pz_agent_voice.driver import VoiceCompanion
from pz_agent_voice.messages import VoiceGoal, VoiceIntent
from pz_agent_voice.ports import VoiceServices
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.voice_doubles import FakeTeamONClient, settle

BUILD: Final = "42.20"

#: Long enough that a loaded runner does not fail a happy path, short enough
#: that a genuine hang ends one test rather than the suite's patience.
GRACE: Final = 10.0

#: The methods of the Local Core RPC link that carry a spoken goal into the
#: sidecar. Two, because ``docs/control/DECISIONS.md`` records the move from the
#: first to the second, and this file asserts that the request arrived rather
#: than which of them carried it.
GOAL_METHODS: Final[tuple[str, ...]] = ("plan.execute", "goal.submit")

#: Where each of those two writes the goal, and what «поешь» has to be by the
#: time it gets there. Hand-written on both sides: a params body derived from
#: the encoder under test would assert that the encoder equals itself.
GOAL_IN_PARAMS: Final[dict[str, tuple[str, str]]] = {
    "plan.execute": ("goal", "eat"),
    "goal.submit": ("kind", "satisfy_hunger"),
}

#: What the fixture core answers ``plan.execute`` with. Not defaults: the status
#: is not the first member of its enum and a step carries both optional keys, so
#: a decoder that dropped a field and fell back cannot pass.
PLAN_ANSWER: Final[JsonDict] = {
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
}

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
# the sidecar side of the goal route
# ---------------------------------------------------------------------------


@dataclass
class Core:
    """A real RPC server on a real socket, and every request it was handed.

    Deliberately not the shipped :class:`~pz_agent_mcp.remote.server.CoreRouter`:
    what is under test here is the CLI's half of the link, and a router would
    bring a session, an engine and a queue along with it — every one of which can
    refuse for its own reasons, none of which are this file's subject.
    """

    state_dir: Path
    server: RpcServer
    thread: threading.Thread
    seen: list[RpcRequest]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.seen]

    def close(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


StartCore = Callable[[Path], Core]


@pytest.fixture
def start_core() -> Iterator[StartCore]:
    """Bring up real cores in the state directories given, and reap them all."""
    started: list[Core] = []

    def _start(state_dir: Path) -> Core:
        runtime = runtime_dir(state_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        seen: list[RpcRequest] = []

        def dispatch(request: RpcRequest) -> RpcResponse:
            seen.append(request)
            if request.method == "plan.execute":
                return RpcResponse(id=request.id, ok=True, result=dict(PLAN_ANSWER))
            if request.method == "goal.status":
                return RpcResponse(
                    id=request.id,
                    ok=True,
                    result={"active": None, "pending": [], "named": None},
                )
            # Anything else is answered honestly rather than with a body this
            # fixture invented: the assertions that matter are about the request
            # that arrived, and a made-up success would be the fabricated answer
            # this project refuses everywhere else.
            return RpcResponse(
                id=request.id,
                ok=False,
                error_code=ErrorCode.UNKNOWN_METHOD,
                error_message=(
                    f"this fixture answers plan.execute and goal.status, not {request.method}"
                ),
            )

        server = RpcServer(new_address(runtime), authkey=key, handler=dispatch)
        write_descriptor(state_dir, server.descriptor())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        core = Core(state_dir=state_dir, server=server, thread=thread, seen=seen)
        started.append(core)
        return core

    yield _start

    for core in started:
        core.close()


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

    The state directory is moved to a one-letter path under *tmp_path*, and not
    for tidiness: the goal route binds a Unix socket inside it, ``sun_path``
    bounds that address to 100 bytes, and the profile directory a
    :func:`~tests.fixtures.cli_worlds.make_world` machine hands out — a Cyrillic
    account name under a pytest temporary directory — is well past it. Every
    other path follows the override, so ``config.toml``, the voice record and the
    logs stay where the commands look for them.
    """
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
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
async def test_a_spoken_goal_arrives_at_a_real_core_over_the_link(
    tmp_path: Path, start_core: StartCore
) -> None:
    """The whole of T002, and the assertion is on what the server received.

    The core is started *after* the companion, which is not incidental: the link
    resolves the descriptor and the token on every call, so a user may say «поешь»
    to a companion that was listening before the sidecar existed. A wiring that
    dialled once at construction would leave ``seen`` empty here.
    """
    with assemble(tmp_path) as live:
        live.arm()
        core = start_core(live.workspace.state_dir)
        queue_before = live.mod.layout.command_queue.read_bytes() if _exists(live) else b""
        await live.start()

        await live.say("агент, поешь")

        turn = live.companion.last_turn
        assert turn is not None
        assert turn.intent is VoiceIntent.GOAL
        assert turn.goal is VoiceGoal.EAT
        queue_after = live.mod.layout.command_queue.read_bytes() if _exists(live) else b""
        await live.finish()

    assert len(core.seen) == 1, f"the core was asked {core.methods}"
    method = core.seen[0].method
    assert method in GOAL_METHODS, f"{method} does not carry a goal into the sidecar"
    key, token = GOAL_IN_PARAMS[method]
    assert core.seen[0].params[key] == token, "the request did not name the goal that was spoken"
    # The other half of the same claim: the microphone reached the core, and it
    # reached it *only* through the core. A goal written into the mod's own queue
    # would be past the reflex guard, the capability gate and the policy engine
    # in one step.
    assert queue_after == queue_before, "a spoken goal reached the command queue"


@pytest.mark.asyncio
async def test_a_spoken_goal_with_no_sidecar_is_refused_out_loud(tmp_path: Path) -> None:
    """No core at all: the companion says so, and still hears the next «стоп».

    This is the state a user is in before ``pz-agent start``, and it is the one
    the removed placeholder made permanent. The difference is that the refusal is
    now the link's — "nothing was listening" — rather than this build's "there is
    no such feature".
    """
    with assemble(tmp_path) as live:
        live.arm()
        await live.start()

        await live.say("агент, поешь")
        refused = [message.text for message in live.adapter.started]

        await live.say("стоп")
        assert live.mod.latch_requested(), "a failed goal left the stop word unheard"
        await live.finish()

    assert refused == [phrases.PLAN_REFUSED]


def _exists(live: Listening) -> bool:
    return live.mod.layout.command_queue.is_file()


def test_voice_check_asks_the_channel_and_reports_what_it_answered(
    tmp_path: Path, start_core: StartCore
) -> None:
    """The command a user runs to find out whether a goal would land.

    Run twice against one machine: once with a core listening and once after it
    has stopped, with its descriptor still on disk naming this very much alive
    process. Anything short of a connection reports both runs the same way, and
    the second would be the answer "routed" about a socket nobody is accepting on.
    """
    world = make_world(tmp_path)
    world.ctx = world.ctx.with_overrides(state_dir=tmp_path / "s")
    core = start_core(resolve_workspace(world.ctx).state_dir)

    assert world.run("voice", "check", "агент", "поешь") == EXIT_OK
    listening = world.stdout

    core.close()
    world.reset_streams()
    assert world.run("voice", "check", "агент", "поешь") == EXIT_OK
    gone = world.stdout

    assert core.methods == ["goal.status"], "the check submitted something to find out"
    assert "reachable" in listening and "UNREACHABLE" not in listening
    assert "UNREACHABLE" in gone
    assert "pz-agent start" in gone


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
    assert record.goals_routed is True, "the companion that ran had a goal route and said it did"


class RecordWatcher(FakeTeamONClient):
    """A recogniser that reads the voice record before it yields anything.

    The record a companion writes when it starts is rewritten when it exits, so
    nothing asserted after the run can see it — and that first record is the one
    ``pz-agent status`` reads in the only state where it matters: while something
    is listening. Opening the transcript stream is the moment the companion is
    up, which is why the look happens here rather than in the test body.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.seen: VoiceRecord | None = None

    async def transcripts(self) -> AsyncIterator[TeamONTranscript]:
        self.seen = read_voice_record(self.path)
        return await super().transcripts()


def test_the_record_a_listening_companion_leaves_says_where_its_goals_go(
    tmp_path: Path,
) -> None:
    """Read from inside the run, because that is when a user would run ``status``."""
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)
    watcher = RecordWatcher(workspace.state_dir / VOICE_RECORD_NAME)

    assert run_voice_run(world.ctx, as_json=False, client=watcher) == EXIT_OK

    assert watcher.seen is not None, "no record existed while the companion was listening"
    assert watcher.seen.running is True
    assert watcher.seen.goals_routed is True
    # And the same fact said out loud to whoever started it.
    assert "Core RPC link" in world.stdout


def test_the_json_a_script_reads_says_where_the_goals_went(tmp_path: Path) -> None:
    """``--json`` is the form a wrapper reads, and it carries the same fact.

    Printed rather than only recorded, because a script that starts the
    companion has no other way to find out whether the goals it is about to
    speak have a channel at all — and a body that said ``false`` while the
    record said ``true`` would be two answers to one question.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]

    assert run_voice_run(world.ctx, as_json=True, client=FakeTeamONClient()) == EXIT_OK

    payload = json.loads(world.stdout)
    assert payload["started"] is True
    assert payload["goals_routed"] is True
    assert payload["ok"] is True


def test_the_log_records_that_the_companion_started_with_a_goal_route(tmp_path: Path) -> None:
    """The half of the answer an operator has after the process is gone.

    The record is rewritten on exit, so ``voice.start`` in the log is the only
    place that still says what the companion had while it was listening — which
    is the question behind "«поешь» did nothing" long after the terminal is
    closed.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)

    assert run_voice_run(world.ctx, as_json=False, client=FakeTeamONClient()) == EXIT_OK

    written = (workspace.logs_dir / "pz-agent.jsonl").read_text(encoding="utf-8")
    starts = [
        record
        for record in (json.loads(line) for line in written.splitlines() if line.strip())
        if record["event"] == "voice.start"
    ]
    assert starts, "nothing in the log says a companion started"
    assert starts[0]["fields"]["goals_routed"] is True


def test_the_companion_writes_the_log_the_debug_map_sends_an_operator_to(
    tmp_path: Path,
) -> None:
    """``docs/LOCAL_DEBUG_MAP.md`` names ``logs/`` for both voice symptoms.

    Both of them — a Russian phrase not recognised, and «стоп» heard while the
    character kept going — and the companion had never written a byte there. Its
    turn history and its synthesiser failures sat in two bounded rings inside a
    process that then exited, with :attr:`VoiceCompanion.speech_failures` saying
    in its own docstring that they are kept because "the companion went quiet"
    with nothing recorded is what a support bundle cannot explain. The bundle
    never saw them. Same shape as the sidecar's own log, one package over.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)

    assert run_voice_run(world.ctx, as_json=False, client=FakeTeamONClient()) == EXIT_OK

    structured = workspace.logs_dir / "pz-agent.jsonl"
    assert structured.is_file(), "the companion left no log for the operator sent to logs/"
    events = {
        json.loads(line)["event"]
        for line in structured.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert "voice.start" in events, "nothing records that a companion started listening"
    assert "voice.finished" in events, "nothing records why it stopped"


def test_the_companion_log_records_intents_and_never_transcripts(tmp_path: Path) -> None:
    """The privacy line this artefact has to hold.

    A support bundle is designed to be attached to a public issue, and a
    microphone's contents are not something to put in one. ``VoiceTurn`` carries
    the *understood* intent and what it caused rather than the words heard, so
    what is written answers "was «стоп» recognised" and "did the stop reach the
    sidecar" without carrying the speech itself.
    """
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)
    spoken = FakeTeamONClient()

    run_voice_run(world.ctx, as_json=False, client=spoken)

    written = (workspace.logs_dir / "pz-agent.jsonl").read_text(encoding="utf-8")
    for record in (json.loads(line) for line in written.splitlines() if line.strip()):
        assert "transcript" not in record["fields"], record
        assert "words" not in record["fields"], record


def test_a_log_directory_that_will_not_take_a_file_does_not_stop_the_companion(
    tmp_path: Path,
) -> None:
    """The companion carries the stop word. A diagnostic is never worth it."""
    world = make_world(tmp_path)
    write_config(world)
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = FAKE_KEY  # type: ignore[index]
    logs = resolve_workspace(world.ctx).logs_dir
    logs.parent.mkdir(parents=True, exist_ok=True)
    # A file where the directory should be: mkdir and every write below it fail.
    logs.write_text("not a directory", encoding="utf-8")

    code = run_voice_run(world.ctx, as_json=False, client=FakeTeamONClient())

    assert code == EXIT_OK, "an unwritable log directory stopped the companion"


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


def a_listening_record(tmp_path: Path, *, goals_routed: bool) -> CliWorld:
    """A machine whose state directory holds the record a running companion wrote."""
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
            goals_routed=goals_routed,
        ),
    )
    return world


def test_status_says_a_companion_is_listening_and_where_its_goals_go(tmp_path: Path) -> None:
    """The record is written by the process that holds the adapter, and read here."""
    world = a_listening_record(tmp_path, goals_routed=True)

    assert world.run("status") == EXIT_OK

    assert "LISTENING" in world.stdout
    assert "pid 4242" in world.stdout
    assert "over the Core RPC link" in world.stdout
    assert "reaches no planner" not in world.stdout


def test_status_still_reads_a_record_written_without_a_goal_route(tmp_path: Path) -> None:
    """A companion from the build that had none, described as it was.

    ``goals_routed`` is a field rather than a sentence in the renderer precisely
    so that this record — which a user may still have in their state directory —
    is reported as what it was instead of being described with today's wiring.
    """
    world = a_listening_record(tmp_path, goals_routed=False)

    assert world.run("status") == EXIT_OK

    assert "LISTENING" in world.stdout
    assert "a spoken goal reaches no planner" in world.stdout


def test_status_reports_voice_in_json_for_a_bug_report(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    write_config(world)

    assert world.run("status", "--json") == EXIT_OK

    assert '"voice"' in world.stdout
    assert f'"adapter": "{ADAPTER_TEAMON}"' in world.stdout
