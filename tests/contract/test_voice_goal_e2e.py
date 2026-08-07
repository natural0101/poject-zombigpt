"""A spoken sentence, all the way into the core's goal channel, over a real link.

Every other test of the voice loop stops at a port. ``tests/unit/test_voice_session.py``
drives a :class:`~pz_agent_voice.session.VoiceSession` over doubles. Those are
green and they cannot tell you whether a goal a user *said* ends up in
:class:`~pz_agent_core.goals.GoalQueue`. That is the gap this file is for, and it
is the gap the project keeps re-discovering: two subsystems that work and a join
nobody exercised.

So the only double here is the microphone
-----------------------------------------

:class:`~pz_agent_voice.adapters.fake.FakeVoiceAdapter` stands in for a
recogniser and a loudspeaker, because a test cannot have either. Everything
between it and the goal is the shipping code:

* the matcher — :func:`~pz_agent_voice.intent.classify` and
  :func:`~pz_agent_voice.intent.is_stop`, reached through the real
  :class:`~pz_agent_voice.session.VoiceSession`;
* the companion — the real :class:`~pz_agent_voice.driver.VoiceCompanion`, with
  its two tasks, its barge-in and its speech pump;
* the wiring — :func:`~pz_agent_voice.plan_port.services_over_core_rpc`, which
  is the function ``pz-agent voice run`` is supposed to call;
* the link — a real :class:`~pz_agent_core.rpc.transport.RpcServer` on a real
  Unix socket or named pipe, a real token, a real descriptor, and the real
  :class:`~pz_agent_mcp.remote.client.RemoteCoreServices` reaching it;
* the router — the real :class:`~pz_agent_mcp.remote.server.CoreRouter`;
* the channel — a real :class:`~pz_agent_core.goals.GoalQueue`, whose admission,
  exclusivity and stop levers are the thing every assertion below is about.

One piece is written here because it does not exist in the tree, and it is named
as a gap rather than smuggled in as scenery: :meth:`Spoken.report_progress` is
one turn of the progress loop a sidecar would run, and no such loop exists yet.

An earlier revision carried a second piece — a ``GoalChannelPlans`` shim that
translated a ``PlanRequest`` into a ``GoalRequest``, because at the time nothing
in the build joined :class:`~pz_agent_voice.messages.VoiceGoal` to
:class:`~pz_agent_core.goals.GoalKind`. The companion does that itself now
(``docs/control/DECISIONS.md``), so the shim is gone. Keeping it would have been
worse than dead code: its table was a *second* copy of the mapping, and a test
could have gone on passing against a translation the shipped companion no longer
performs. :data:`KIND_FOR_SPOKEN_GOAL` survives on purpose — it is this file's
own independently written statement of the same mapping, checked against the
product rather than imported from it.

What the fake adapter cannot tell you
-------------------------------------

:class:`TestTheSuiteFailsWhenTheGoalNeverReachesTheCore` is the load-bearing
class. It runs the same companion twice more — once with the link severed, once
with the link up and answered by a port that accepts every goal and admits none
of them — and in both runs every *structural* observation a microphone-side test
makes still holds: the transcript classifies, the turn carries the goal, the
companion speaks, nothing is cut short and nothing raises. (The severed run says
a different sentence, and the assertions say so rather than claiming the two
runs are indistinguishable; the cheerful-port run is indistinguishable down to
the sentence and the plan record.) Then each asserts that
:func:`assert_the_core_received`, the same function the passing tests call,
raises :class:`AssertionError`. A suite that only watched the adapter would be
green in all three runs, and that is precisely the failure this file exists to
make impossible.

Bounded, because a socket and two threads are involved: every join, every wait
and every settle has a limit, and a server that is not reaped fails a test
instead of outliving the run.
"""

from __future__ import annotations

# Two docstrings below quote sentences this build says out loud, in Russian and
# short enough that every letter in them has a Latin lookalike — which the
# confusable-character rule reads as mistyped ASCII. Taking its suggestion would
# leave a docstring quoting a sentence the companion never says.
# ruff: noqa: RUF002
import asyncio
import itertools
import json
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.goals import (
    GOAL_SPECS,
    GoalAdmission,
    GoalKind,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    key_digest,
)
from pz_agent_core.protocol import ActionName, ActionStatus, ReasonCode, SessionMode
from pz_agent_core.rpc.descriptor import write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from pz_agent_mcp.ports import (
    CoreServices,
    GoalCancellation,
    GoalChannelStatus,
    GoalPort,
    PlanRecord,
    SessionSnapshot,
    StopReport,
)
from pz_agent_mcp.remote.client import RemoteCoreServices
from pz_agent_mcp.remote.server import CoreRouter
from pz_agent_voice import phrases
from pz_agent_voice.adapters.fake import FakeVoiceAdapter
from pz_agent_voice.driver import VoiceCompanion
from pz_agent_voice.messages import VoiceGoal, VoiceIntent
from pz_agent_voice.plan_port import MAX_VOICE_PLAN_DEADLINE_SECONDS, services_over_core_rpc
from pz_agent_voice.ports import VoiceServices
from pz_agent_voice.session import VoiceSession
from pz_agent_voice.state import VoiceTurn
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.mcp_doubles import (
    CountingIds,
    Doubles,
    FakeSessionPort,
    succeeded_result,
)
from tests.fixtures.voice_doubles import HOSTILE_TRANSCRIPT, settle

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------

#: How long a thread holding a socket is given to go away. Long enough for a
#: loaded runner, short enough that a wedged server ends the test rather than
#: the run.
GRACE: Final = 10.0

#: The deadline the voice path dials with here. Deliberately the *ceiling*
#: :mod:`~pz_agent_voice.plan_port` allows rather than its three-second default:
#: the default is a bound on a person's patience and is pinned by the unit test
#: that owns it, while the number that matters here is the one that keeps a
#: loaded CI worker from failing a test about goals for a reason about timing.
DEADLINE: Final = MAX_VOICE_PLAN_DEADLINE_SECONDS

#: Rounds of scheduler yielding a single transcript is given. Bounded so a
#: companion that deadlocks fails an assertion instead of hanging the suite.
SETTLE_ROUNDS: Final = 8

#: Requests the wire tap keeps. A test asserts over a handful; anything more is
#: a leak in a fixture that lives as long as the test session.
WIRE_KEEP: Final = 32

# ---------------------------------------------------------------------------
# the contract, as literals
# ---------------------------------------------------------------------------
#
# Hand-written rather than derived. A test that computes its expectation from
# the code under test agrees with that code however it changes.

#: Which goal kind each spoken goal becomes.
#:
#: The product does this now — ``pz_agent_voice.session._GOAL_KIND`` — and this
#: table is deliberately NOT imported from there. It is this file's independent
#: statement of the same mapping, so a row changed on one side and not the other
#: fails here. A test that asserted the product's table against itself would
#: agree with any mapping at all, including a wrong one.
#: ``RESUME`` maps to nothing on purpose: resuming is a re-arming, not a goal,
#: and inventing a kind for it would widen a closed channel by a member the
#: planner cannot serve.
KIND_FOR_SPOKEN_GOAL: Final[Mapping[VoiceGoal, GoalKind | None]] = {
    VoiceGoal.EAT: GoalKind.SATISFY_HUNGER,
    VoiceGoal.DRINK: GoalKind.SATISFY_THIRST,
    VoiceGoal.READ: GoalKind.READ_FOR_BOREDOM,
    VoiceGoal.RESUME: None,
}

#: How a goal's state reads as a plan's status on the way back to the companion.
#: ``EXPIRED`` becomes ``FAILED`` rather than gaining a status of its own: the
#: distinction matters to a log and not to a loudspeaker, and
#: :class:`~pz_agent_core.protocol.ActionStatus` has no member for it.
PLAN_STATUS_FOR_GOAL_STATE: Final[Mapping[GoalState, ActionStatus]] = {
    GoalState.PENDING: ActionStatus.ACCEPTED,
    GoalState.ACTIVE: ActionStatus.STARTED,
    GoalState.SUCCEEDED: ActionStatus.SUCCEEDED,
    GoalState.FAILED: ActionStatus.FAILED,
    GoalState.CANCELLED: ActionStatus.CANCELLED,
    GoalState.EXPIRED: ActionStatus.FAILED,
}

#: One row per goal a user can speak: what they say, what it classifies to, what
#: kind the core must end up holding, and that kind's wire value written out by
#: hand so a uniformly wrong table cannot agree with itself.
SPOKEN_GOALS: Final[tuple[tuple[str, VoiceGoal, GoalKind, str], ...]] = (
    ("Агент, поешь", VoiceGoal.EAT, GoalKind.SATISFY_HUNGER, "satisfy_hunger"),
    ("Агент, попей", VoiceGoal.DRINK, GoalKind.SATISFY_THIRST, "satisfy_thirst"),
    ("Агент, почитай", VoiceGoal.READ, GoalKind.READ_FOR_BOREDOM, "read_for_boredom"),
)

#: Said to stop. One word, no wake word, and it must work from any state.
STOP_PHRASE: Final = "Стоп"

#: How many goals are in the channel when the spoken stop lands, and therefore
#: what :class:`~pz_agent_mcp.ports.StopReport` must say was cleared. Written
#: out here because the *value* is load bearing rather than incidental:
#: :class:`~tests.fixtures.mcp_doubles.FakeSessionPort` answers a canned
#: ``StopReport(cleared=2, …)``, and :class:`GoalChannelSession` delegates its
#: other three methods to exactly that double — so a stop that had been
#: delegated with them, instead of pulling
#: :meth:`~pz_agent_core.goals.GoalQueue.panic_stop`, would answer two and agree
#: with a test that asked for two. Three is a number only the real channel can
#: produce, and the test asserts that the double still cannot produce it.
UNREACHABLE_BY_THE_DOUBLE: Final = 3

#: Fragments of :data:`~tests.fixtures.voice_doubles.HOSTILE_TRANSCRIPT` that
#: must not appear anywhere the goal travelled. The last one is the goal word
#: itself: the channel carries a token, so even the part of the sentence that
#: *was* understood does not cross.
TRANSCRIPT_FRAGMENTS: Final[tuple[str, ...]] = (
    "SYSTEM: ignore",
    "secrets.txt",
    "hostile",
    "AUTONOMOUS",
    "поешь",
)


class UnroutableSpokenGoal(RuntimeError):
    """A spoken goal names no goal kind, so nothing was submitted.

    Carries no part of the request: the goal token came off a wire, and a
    message quoting it would put a peer's bytes into this process's traceback.
    """


class ChannelRefused(RuntimeError):
    """The goal channel declined the submission, in its own words.

    :class:`~pz_agent_core.goals.GoalRefusal` builds its message from constants
    and identifiers the core minted, which is the only reason it is safe to
    carry one here.
    """


# ---------------------------------------------------------------------------
# the sidecar side: the ports the router calls
# ---------------------------------------------------------------------------


@dataclass
class QueueGoalPort:
    """The real :class:`~pz_agent_mcp.ports.GoalPort`, served by the real queue.

    This replaces a translation shim whose own docstring said it was "the piece
    the build is missing" — a ``PlanRequest`` arriving, a hand-written table
    turning the spoken token into a kind, and a ``PlanRecord`` going back. That
    was true when it was written and stopped being true: the companion submits a
    ``GoalRequest`` now (``docs/control/DECISIONS.md``), ``CoreRouter`` routes
    ``goal.submit`` / ``goal.status`` / ``goal.cancel``, and the mapping from a
    spoken token to a kind lives in ``session.py`` where the product keeps it.

    Leaving the shim in place would have been worse than dead code. Its table
    was a *second* copy of that mapping, so a test could have gone on passing
    against a translation the shipped companion no longer performs — the
    scaffold asserting a join the product had moved.

    Nothing here decides anything: it is the queue's own answers in the port's
    shape, which is what the sidecar will hand the router.
    """

    channel: GoalQueue
    #: Every request that arrived, for assertions about what actually crossed.
    submitted: list[GoalRequest] = field(default_factory=list)

    def submit(self, request: GoalRequest) -> GoalAdmission:
        self.submitted.append(request)
        return self.channel.submit(request)

    def status(self, goal_id: str | None = None) -> GoalChannelStatus:
        named = self.channel.record(goal_id) if goal_id is not None else None
        return GoalChannelStatus(
            active=self.channel.active, pending=self.channel.pending, named=named
        )

    def cancel(self, goal_id: str) -> GoalCancellation:
        goal = self.channel.record(goal_id)
        if goal is None:
            return GoalCancellation(goal=None, requested=False)
        requested = self.channel.request_cancel(goal_id)
        return GoalCancellation(goal=self.channel.record(goal_id) or goal, requested=requested)


@dataclass
class GoalChannelSession:
    """The session port, with the stop lever wired to the real channel.

    ``status``, ``arm`` and ``disarm`` delegate to
    :class:`~tests.fixtures.mcp_doubles.FakeSessionPort`, because a real session
    port reads a heartbeat written by a running game and there is none here.
    ``stop`` does not delegate: it pulls
    :meth:`~pz_agent_core.goals.GoalQueue.panic_stop`, so "the user said стоп"
    and "the goal ended" are joined by the production lever rather than by a
    counter this file increments.
    """

    channel: GoalQueue
    inner: Doubles
    stops: int = 0

    def status(self) -> SessionSnapshot:
        return self.inner.session.status()

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        self.channel.arm()
        return self.inner.session.arm(mode, confirm_backup=confirm_backup)

    def disarm(self) -> SessionSnapshot:
        self.channel.disarm()
        return self.inner.session.disarm()

    def stop(self) -> StopReport:
        self.stops += 1
        ended = self.channel.panic_stop()
        snapshot = self.inner.session.disarm()
        return StopReport(cleared=len(ended), disarmed=True, mode=snapshot.mode)


def _as_plan(record: GoalRecord) -> PlanRecord:
    """One goal, as the plan record the companion polls for.

    ``plan_id`` is the goal id, so the identity the user's companion tracks is
    the identity the channel minted; a second id would be a second thing to keep
    in step.
    """
    return PlanRecord(
        plan_id=record.goal_id,
        status=PLAN_STATUS_FOR_GOAL_STATE[record.state],
        step_index=record.steps_used,
        stopped_reason=record.reason_code,
    )


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------

#: Servers started by `serve`, closed by the autouse fixture below so a test
#: that raises does not leave a thread holding a socket.
_SERVERS: list[tuple[RpcServer, threading.Thread]] = []
_COUNTER = itertools.count()


@pytest.fixture(autouse=True)
def _reap_servers() -> Iterator[None]:
    """Close every server this test started, and prove each thread went away.

    The join is not enough on its own: :meth:`~threading.Thread.join` with a
    timeout returns whether or not the thread ended, so a server that ignored
    :meth:`~pz_agent_core.rpc.transport.RpcServer.close` would leave a daemon
    thread holding a socket and this fixture would say nothing about it. The
    survivors are collected first and asserted after the loop, so one wedged
    server does not stop the rest being closed.
    """
    yield
    survivors = 0
    while _SERVERS:
        server, thread = _SERVERS.pop()
        server.close()
        thread.join(timeout=GRACE)
        if thread.is_alive():
            survivors += 1
    assert survivors == 0, f"{survivors} server thread(s) outlived the test that started them"


class WireTap:
    """The real router, with the params of each call kept for inspection.

    A wrapper rather than a subclass: :class:`~pz_agent_mcp.remote.server.CoreRouter`
    decides nothing and must keep deciding nothing, so this adds a list and
    delegates. The list is appended from the server thread and read from the
    test thread after the call it records has returned, which is what makes a
    plain list enough.
    """

    def __init__(self, router: CoreRouter) -> None:
        self._router = router
        #: ``(method, params as they arrived)``, bounded by :data:`WIRE_KEEP`.
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: RpcRequest) -> RpcResponse:
        if len(self.calls) < WIRE_KEEP:
            self.calls.append(
                (request.method, json.dumps(request.params, ensure_ascii=False, sort_keys=True))
            )
        return self._router(request)


@dataclass
class Link:
    """A running sidecar, as the companion would find one."""

    channel: GoalQueue
    goals: GoalPort
    session: GoalChannelSession
    wire: WireTap
    state_dir: Path
    server: RpcServer
    thread: threading.Thread

    def sever(self) -> None:
        """Cut the link. Safe to call twice; the reaper calls it again."""
        self.server.close()
        self.thread.join(timeout=GRACE)

    def activate(self) -> GoalRecord | None:
        """One turn of the sidecar's activation loop. The record, or None."""
        return self.channel.activate_next().goal


def arrived_at(link: Link) -> list[GoalRequest]:
    """Every request the goal-channel translation received on the far side.

    Narrowed here rather than at each call site: :attr:`Link.goals` is declared
    as the port so that a link serving something else — the cheerful port in
    T006 — is the same type, and a test asking this of that link is a test with
    a bug rather than one with an empty list.
    """
    goals = link.goals
    goals = link.goals
    assert isinstance(goals, QueueGoalPort), "this link does not serve the goal channel"
    return goals.submitted


def serve(tmp_path: Path, *, channel: GoalQueue, goals: GoalPort) -> Link:
    """Stand a core up behind a real socket, serving *goals* over *channel*."""
    state_dir = tmp_path / f"pz-agent-{next(_COUNTER)}"
    runtime = state_dir / "runtime"
    runtime.mkdir(parents=True)
    key = issue_token(runtime)

    doubles = Doubles()
    session = GoalChannelSession(channel=channel, inner=doubles)
    services: CoreServices = replace(doubles.services, session=session, goals=goals)
    # goals= is passed explicitly. CoreRouter does NOT read services.goals: the
    # field is Optional and the router may contain no comparison at all (its own
    # test reads its AST and fails on a single If node), so choosing between a
    # channel and the absent one is a decision that lives at the call site. The
    # sidecar makes it too; this is that call site for the test.
    wire = WireTap(CoreRouter(services, goals=goals))

    server = RpcServer(new_address(runtime), authkey=key, handler=wire)
    write_descriptor(state_dir, server.descriptor())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVERS.append((server, thread))
    return Link(
        channel=channel,
        goals=goals,
        session=session,
        wire=wire,
        state_dir=state_dir,
        server=server,
        thread=thread,
    )


@pytest.fixture
def link(tmp_path: Path) -> Link:
    """The whole core: a real goal channel behind a real router and socket."""
    channel = GoalQueue(clock=FakeClock())
    return serve(tmp_path, channel=channel, goals=QueueGoalPort(channel=channel))


# ---------------------------------------------------------------------------
# the companion side
# ---------------------------------------------------------------------------


@dataclass
class Spoken:
    """A running companion and the microphone it is listening to."""

    clock: FakeClock
    adapter: FakeVoiceAdapter
    services: VoiceServices
    session: VoiceSession
    companion: VoiceCompanion
    task: asyncio.Task[None] | None = None
    #: The goal this progress loop is following, so a finished one is still
    #: asked about by id rather than lost when it leaves the active slot.
    _tracking: str | None = None

    @property
    def said(self) -> list[str]:
        """Every sentence the adapter was asked to speak, in order."""
        return [message.text for message in self.adapter.started]

    async def start(self) -> None:
        self.task = asyncio.create_task(self.companion.run())
        await settle()

    async def say(self, transcript: str) -> VoiceTurn:
        """Speak *transcript* into the microphone and return the turn it caused.

        Bounded: the companion is given a fixed number of scheduler rounds to
        handle the transcript, so a wedged plan port fails here rather than
        parking the suite.
        """
        before = len(self.companion.turns)
        self.adapter.push(transcript)
        for _ in range(SETTLE_ROUNDS):
            await settle()
            if len(self.companion.turns) > before:
                break
        turn = self.companion.last_turn
        assert len(self.companion.turns) > before, "the companion never handled the transcript"
        assert turn is not None
        # One more round so the speech pump gets to say what the turn queued.
        await settle()
        return turn

    async def report_progress(self) -> GoalRecord | None:
        """One turn of the progress loop a sidecar would run.

        Reads the goal channel over the link and hands the record to
        :meth:`~pz_agent_voice.session.VoiceSession.report_goal`, which decides
        whether it is news. No such loop exists in the build yet; this is the
        smallest thing that puts a record the *core* produced in front of the
        companion, which is what "the user is told" has to mean.

        It used to read ``plans.current()``. That went silent the moment the
        companion started submitting to the goal channel — the planner had
        nothing to report because nothing had asked it — and the goal ran to
        completion with the user never told. ``report_goal`` did not exist then;
        that gap was the finding, and this reads the channel the goal is
        actually on.

        ``named`` is preferred over ``active`` so a goal that has already
        finished is still reported: a poll that only ever looked at the active
        slot would miss every goal that ended between two polls, which is most
        of them.
        """
        goals = self.services.goals
        assert goals is not None, "this companion was assembled without a goal channel"
        status = goals.status(self._tracking)
        record = status.named or status.active
        if record is not None:
            self._tracking = record.goal_id
            self.session.report_goal(record)
        await settle()
        return record


def _build(link: Link) -> Spoken:
    """The companion, wired the way ``pz-agent voice run`` is meant to wire it."""
    clock = FakeClock()
    remote = RemoteCoreServices.from_state_dir(link.state_dir, deadline=DEADLINE)
    services = services_over_core_rpc(remote.session, link.state_dir, deadline=DEADLINE)
    session = VoiceSession(services, clock=clock, ids=CountingIds("voice"))
    adapter = FakeVoiceAdapter(clock=clock)
    return Spoken(
        clock=clock,
        adapter=adapter,
        services=services,
        session=session,
        companion=VoiceCompanion(adapter, session, clock=clock),
    )


@asynccontextmanager
async def companion_over(link: Link) -> AsyncIterator[Spoken]:
    """A started companion over *link*, shut down bounded on the way out."""
    spoken = _build(link)
    await spoken.start()
    try:
        yield spoken
    finally:
        spoken.adapter.close()
        task = spoken.task
        if task is not None:
            for _ in range(SETTLE_ROUNDS):
                await settle()
                if task.done():
                    break
            if not task.done():
                task.cancel()
                raise AssertionError("the companion did not shut down after its stream closed")
            await task


# ---------------------------------------------------------------------------
# the assertion T006 severs the link under
# ---------------------------------------------------------------------------


def assert_the_core_received(link: Link, kind: GoalKind, *, wire_value: str) -> GoalRecord:
    """Fail unless the core is holding exactly one goal of *kind*, and start it.

    Written once and called from four tests on purpose. Three of them expect it
    to pass; :class:`TestTheSuiteFailsWhenTheGoalNeverReachesTheCore` expects it
    to raise, and it has to be the *same* function or that class would prove
    nothing about the checks the other three run.

    Everything it reads comes from the channel: the count of open goals, the
    admission the channel's own activation answered, and the kind on the record
    the channel minted. Nothing here consults the adapter, the turn or the
    companion, which is what makes it blind to a microphone that is having a
    lovely time.
    """
    assert link.channel.open_count == 1, (
        f"the core holds {link.channel.open_count} open goal(s), not the one that was spoken"
    )
    record = link.activate()
    assert record is not None, "the core would not start the goal it was sent"
    assert record.state is GoalState.ACTIVE
    assert record.kind is kind
    assert record.kind.value == wire_value, "the kind the core holds is not the one asked for"
    return record


# ---------------------------------------------------------------------------
# T001 — a spoken phrase produces a goal the core receives
# ---------------------------------------------------------------------------


class TestASpokenPhraseReachesTheCore:
    def test_the_tables_this_file_pins_cover_every_member(self) -> None:
        """A closed set that grew a member must fail here, not at a microphone.

        Both tables below are this file's own; a kind or a state added to the
        core with no row would otherwise be a ``KeyError`` inside a server
        thread, which surfaces as ``CORE_REFUSED`` and reads as a link problem.
        """
        assert set(KIND_FOR_SPOKEN_GOAL) == set(VoiceGoal)
        assert set(PLAN_STATUS_FOR_GOAL_STATE) == set(GoalState)
        assert {row[1] for row in SPOKEN_GOALS} | {VoiceGoal.RESUME} == set(VoiceGoal)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("transcript", "goal", "kind", "wire_value"), SPOKEN_GOALS)
    async def test_the_goal_the_core_holds_is_the_one_that_was_spoken(
        self, link: Link, transcript: str, goal: VoiceGoal, kind: GoalKind, wire_value: str
    ) -> None:
        """Microphone, matcher, companion, socket, router, channel.

        Three phrases rather than one: a translation that answered a single
        constant would satisfy any one of them, and the kinds differ only in a
        table lookup that is easy to get uniformly wrong.
        """
        async with companion_over(link) as spoken:
            turn = await spoken.say(transcript)

        assert turn.intent is VoiceIntent.GOAL
        assert turn.goal is goal

        record = assert_the_core_received(link, kind, wire_value=wire_value)
        assert record.goal_id, "the channel minted no id for the goal it admitted"

        assert [request.kind for request in arrived_at(link)] == [kind], (
            "the far side did not receive exactly the kind the spoken token maps to"
        )
        assert phrases.GOAL_ACCEPTED[goal] in spoken.said

    @pytest.mark.asyncio
    async def test_the_call_that_carried_it_was_goal_submit(self, link: Link) -> None:
        """The goal took the declared method, not some second route.

        Asserted against the literal name rather than
        :class:`~pz_agent_mcp.remote.methods.Method`, so a renamed method is a
        protocol change this test notices instead of one it follows.

        This used to assert ``plan.execute``. The companion submits to the typed
        goal channel now (``docs/control/DECISIONS.md``), and the *negative* half
        below is the part that matters: a build that submitted on both routes, or
        fell back to the planner when the channel refused, would satisfy a bare
        "goal.submit is present" check and is caught here.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")

        called = [method for method, _ in link.wire.calls]
        assert "goal.submit" in called
        assert "plan.execute" not in called, (
            "the companion also called the planner; a spoken goal takes one route "
            f"and the wire shows {called}"
        )
        assert_the_core_received(link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger")

    @pytest.mark.asyncio
    async def test_no_transcript_byte_crosses_the_link(self, link: Link) -> None:
        """The channel is one token wide, and this is where that is observable.

        The transcript carries an injected instruction, a mode name it would
        like armed and a Windows path with an account in it. What crosses is
        ``eat``. The assertion is over the params as they arrived on the far
        side of the socket — not over a field called ``transcript``, because a
        leak would never be in that one.
        """
        async with companion_over(link) as spoken:
            turn = await spoken.say(HOSTILE_TRANSCRIPT)

        assert turn.goal is VoiceGoal.EAT, "the matcher did not find the goal in the sentence"
        record = assert_the_core_received(
            link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger"
        )

        haystacks = [params for _, params in link.wire.calls]
        haystacks.extend([repr(record), repr(link.channel.active), *spoken.said])
        for haystack in haystacks:
            for fragment in TRANSCRIPT_FRAGMENTS:
                assert fragment not in haystack, f"{fragment!r} survived the crossing"
        assert "voice-1" not in repr(record), "the raw idempotency key is on the record"

    @pytest.mark.asyncio
    async def test_a_spoken_goal_with_no_kind_is_refused_rather_than_invented(
        self, link: Link
    ) -> None:
        """``продолжай`` is a re-arming, not a goal, and the core has no kind for it.

        The interesting half is the second assertion: the channel stays empty.
        A translation that fell back to some default kind would leave the user
        told "продолжаю" while the agent ate a tin of beans.
        """
        async with companion_over(link) as spoken:
            turn = await spoken.say("Агент, продолжай")

        assert turn.goal is VoiceGoal.RESUME
        assert link.channel.open_count == 0, "a goal with no kind was admitted anyway"
        assert phrases.PLAN_REFUSED in spoken.said
        assert phrases.GOAL_ACCEPTED[VoiceGoal.RESUME] not in spoken.said


# ---------------------------------------------------------------------------
# T002 — the user is told when the goal ends
# ---------------------------------------------------------------------------


class TestTheUserIsToldWhenTheGoalEnds:
    @pytest.mark.asyncio
    async def test_the_end_of_the_goal_comes_back_as_a_sentence(self, link: Link) -> None:
        """The core ends the goal; the user hears it, through the companion.

        The order is the whole test. While the goal is running the companion is
        polled and says nothing — a companion that announced "готово" on every
        poll would pass an assertion that only looked at the end. Then the
        channel is given observed evidence, the same poll runs again, and the
        sentence arrives at the *adapter*, which is the only place a user would
        ever hear it.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            record = assert_the_core_received(
                link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger"
            )

            running = await spoken.report_progress()
            assert running is not None
            assert running.goal_id == record.goal_id, "the id the user tracks is not the core's"
            assert running.is_open, "the goal was reported finished while it was still running"
            assert phrases.PLAN_DONE not in spoken.said, "the end was announced before it happened"

            ended = link.channel.succeed(
                record.goal_id,
                succeeded_result(ActionName.CONSUME_EAT, observed={"hunger_after": 0.1}),
            )
            assert ended is not None
            assert ended.state is GoalState.SUCCEEDED

            finished = await spoken.report_progress()

        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert phrases.PLAN_DONE in spoken.said, "the goal ended and the user was never told"

    @pytest.mark.asyncio
    async def test_a_goal_the_core_failed_is_reported_as_that_failure(self, link: Link) -> None:
        """Not merely "something ended": the reason crosses the link too.

        ``NO_SAFE_FOOD`` has its own sentence in
        :data:`~pz_agent_voice.phrases.REFUSAL_SPEECH`; a reason that was lost
        in transit would arrive as the generic refusal, which is a different
        thing to be told and one the user cannot act on.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            record = assert_the_core_received(
                link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger"
            )
            await spoken.report_progress()

            link.channel.fail(record.goal_id, ReasonCode.NO_SAFE_FOOD, "nothing safe to eat")
            reported = await spoken.report_progress()

        assert reported is not None
        assert reported.reason_code is ReasonCode.NO_SAFE_FOOD
        assert phrases.refusal(ReasonCode.NO_SAFE_FOOD) in spoken.said
        assert phrases.PLAN_DONE not in spoken.said, "a failed goal was reported as done"


# ---------------------------------------------------------------------------
# T003 — a spoken cancel ends the active goal
# ---------------------------------------------------------------------------


class TestASpokenCancelEndsTheActiveGoal:
    @pytest.mark.asyncio
    async def test_the_stop_word_ends_the_goal_in_the_core(self, link: Link) -> None:
        """One word, no wake word, and the goal the core was running is over.

        The control matters as much as the result: the goal is asserted
        *active* before the stop, so "no goal is active" afterwards is a change
        of state rather than the state it was always in. The reason code is
        asserted too — a goal that ended because its budget ran out in the same
        window would satisfy the first assertion and mean nothing.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            record = assert_the_core_received(
                link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger"
            )
            running = link.channel.active
            assert running is not None, "nothing was running to be cancelled"
            assert running.goal_id == record.goal_id

            turn = await spoken.say(STOP_PHRASE)

        assert turn.intent is VoiceIntent.STOP
        assert link.session.stops == 1, "the stop never reached the core's session port"
        assert link.channel.active is None, "the goal survived the spoken stop"

        ended = link.channel.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.CANCELLED
        assert ended.reason_code is ReasonCode.PANIC_STOP
        assert ended.is_open is False
        assert link.channel.open_count == 0

    @pytest.mark.asyncio
    async def test_the_count_the_user_hears_about_is_the_core_s_own(self, link: Link) -> None:
        """The stop report crossed the link carrying what was actually cleared.

        *Three* goals are put in the channel — one active, two waiting — and the
        number three is the whole point. Two would have been the wrong choice:
        :attr:`~tests.fixtures.mcp_doubles.FakeSessionPort.stop_report` is
        canned as ``cleared=2``, so a :class:`GoalChannelSession` that had
        quietly delegated ``stop`` to the double instead of pulling
        :meth:`~pz_agent_core.goals.GoalQueue.panic_stop` would answer two and
        satisfy the assertion by coincidence. Three is a number only the channel
        can produce, and :data:`UNREACHABLE_BY_THE_DOUBLE` fails this test if
        that ever stops being true.

        The acknowledgement is spoken only after the port answered, so hearing
        it at all is evidence the round trip completed.
        """
        assert FakeSessionPort().stop_report.cleared != UNREACHABLE_BY_THE_DOUBLE, (
            "the session double now answers this count on its own, so the assertion below "
            "would no longer distinguish the core's number from the double's; pick another"
        )

        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            assert_the_core_received(link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger")
            await spoken.say("Агент, попей")
            await spoken.say("Агент, почитай")
            assert link.channel.open_count == UNREACHABLE_BY_THE_DOUBLE, (
                "the backlog did not grow, so the count below would prove nothing"
            )

            turn = await spoken.say(STOP_PHRASE)

        report = turn.stop
        assert report is not None, "the companion said it stopped without a report"
        assert report.cleared == UNREACHABLE_BY_THE_DOUBLE
        assert report.disarmed is True
        assert phrases.STOP_ACK in spoken.said
        assert phrases.STOP_FAILED not in spoken.said
        assert link.channel.open_count == 0, "the channel the count came from is not empty"

    @pytest.mark.asyncio
    async def test_an_interim_stop_is_honoured_before_the_recogniser_settles(
        self, link: Link
    ) -> None:
        """A guess may cancel work; nothing else may act on one.

        The interim transcript is the only path by which the stop beats the
        recogniser's endpointing delay, and it is the one place where "only
        final transcripts are acted on" would silently disable the safety
        lever.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            assert_the_core_received(link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger")

            spoken.adapter.push(STOP_PHRASE, final=False, confidence=0.2)
            for _ in range(SETTLE_ROUNDS):
                await settle()
                if link.session.stops:
                    break

        assert link.session.stops == 1, "an interim stop never reached the core"
        assert link.channel.active is None
        assert link.channel.open_count == 0


# ---------------------------------------------------------------------------
# T006 — the suite fails when the goal never reaches the core
# ---------------------------------------------------------------------------


@dataclass
class CheerfulGoalPort:
    """A goal port that accepts everything and serves nothing.

    The most dangerous shape a fake takes: it answers the way a working channel
    answers — a real ``GoalAdmission`` carrying a real ``GoalRecord`` with an id
    and ``PENDING`` — so every companion-side assertion passes. It simply never
    tells the queue. This is what "a fake adapter answering happily is not an
    integration" means concretely, and the test below is the only thing that can
    tell it from the real port.
    """

    submitted: list[GoalRequest] = field(default_factory=list)
    _issued: int = 0

    def submit(self, request: GoalRequest) -> GoalAdmission:
        self.submitted.append(request)
        self._issued += 1
        return GoalAdmission(
            goal=GoalRecord(
                goal_id=f"cheerful-{self._issued:04d}",
                kind=request.kind,
                params=request.params,
                budget=request.budget or GOAL_SPECS[request.kind].budget,
                key_digest=key_digest(request.idempotency_key),
                sequence=self._issued,
                state=GoalState.PENDING,
                submitted_at_ms=0,
            )
        )

    def status(self, goal_id: str | None = None) -> GoalChannelStatus:
        return GoalChannelStatus()

    def cancel(self, goal_id: str) -> GoalCancellation:
        return GoalCancellation(goal=None, requested=False)


class TestTheSuiteFailsWhenTheGoalNeverReachesTheCore:
    """The decisive one. A green fake adapter must not be able to hide this.

    The tests here run the same companion over the same fake microphone as the
    passing tests above, assert that the microphone-side observations a
    companion test would make still hold, and then assert that
    :func:`assert_the_core_received` raises. That is what "the suite reports
    failure" means concretely: the function the passing tests are built on says
    no, in the situations where the goal did not land.
    """

    @pytest.mark.asyncio
    async def test_a_severed_link_leaves_the_microphone_green_and_the_core_empty(
        self, link: Link
    ) -> None:
        """The sidecar is gone. The companion neither notices nor stops working.

        That is by design — :mod:`~pz_agent_voice.plan_port` is explicit that a
        refused goal must arrive as a sentence rather than as a companion that
        failed to start — and it is exactly why watching the adapter proves
        nothing. Everything below the sever is asserted to still be true.
        """
        link.sever()

        async with companion_over(link) as spoken:
            turn = await spoken.say("Агент, поешь")

        # Green, on every surface a fake adapter can see.
        assert turn.intent is VoiceIntent.GOAL
        assert turn.goal is VoiceGoal.EAT
        assert spoken.said, "the companion went silent, which is a different failure"
        assert spoken.adapter.started == spoken.adapter.spoken, "an utterance was cut short"
        assert spoken.companion.speech_failures == ()

        # And wrong, on the only surface that matters.
        assert link.channel.open_count == 0
        with pytest.raises(AssertionError, match="open goal"):
            assert_the_core_received(link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger")

    @pytest.mark.asyncio
    async def test_a_port_that_answers_happily_is_still_not_an_integration(
        self, tmp_path: Path
    ) -> None:
        """The link is up, the far side accepts, and the channel is untouched.

        This is the sharper half. Nothing is severed and nothing raises: the
        companion submits the goal, gets a plan record back with an id and an
        ``ACCEPTED`` status, and says the acceptance sentence. A test written
        against the companion — the turn, the adapter, the plan record, even the
        far side having *received* the request — passes every one of those
        assertions. The goal channel is still empty, and this is where that is
        caught.
        """
        channel = GoalQueue(clock=FakeClock())
        cheerful = CheerfulGoalPort()
        happy = serve(tmp_path, channel=channel, goals=cheerful)

        async with companion_over(happy) as spoken:
            turn = await spoken.say("Агент, поешь")

        # Every assertion a companion-side test would make.
        assert turn.intent is VoiceIntent.GOAL
        assert turn.goal is VoiceGoal.EAT
        assert phrases.GOAL_ACCEPTED[VoiceGoal.EAT] in spoken.said, turn.detail
        assert [request.kind for request in cheerful.submitted] == [GoalKind.SATISFY_HUNGER], (
            "the request did not even reach the far side of the link"
        )

        # And the one it could not make.
        assert channel.open_count == 0
        with pytest.raises(AssertionError, match="open goal"):
            assert_the_core_received(happy, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger")

    @pytest.mark.asyncio
    async def test_a_stop_that_reaches_nothing_is_caught_the_same_way(self, link: Link) -> None:
        """The cancel path gets the same treatment as the goal path.

        A severed stop is the worst version of this failure: the companion says
        «Остановился.» only after its port answers, so a link that is down
        produces «Не смог остановить.» instead — and a suite that never asked
        the core would read either sentence as a working stop.
        """
        async with companion_over(link) as spoken:
            await spoken.say("Агент, поешь")
            record = assert_the_core_received(
                link, GoalKind.SATISFY_HUNGER, wire_value="satisfy_hunger"
            )
            link.sever()
            turn = await spoken.say(STOP_PHRASE)

        assert turn.intent is VoiceIntent.STOP
        assert phrases.STOP_FAILED in spoken.said
        assert phrases.STOP_ACK not in spoken.said, "a stop that never landed was acknowledged"

        assert link.session.stops == 0, "the core's stop lever was somehow pulled"
        still_running = link.channel.record(record.goal_id)
        assert still_running is not None
        assert still_running.state is GoalState.ACTIVE, (
            "the goal ended without the core's stop lever being pulled"
        )
