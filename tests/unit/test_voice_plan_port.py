"""The companion's plan port, against a real core over a real Core RPC link.

Not a double. Every test that claims a goal reaches the core starts an
:class:`~pz_agent_core.rpc.transport.RpcServer` on a thread, writes a real
descriptor and a real token, and lets
:func:`~pz_agent_voice.plan_port.core_plan_port` find its way there the way
``pz-agent voice run`` will. The claims under test — a goal is submitted rather
than refused unasked, a stop is honoured with the core unreachable, a wedged
core produces a sentence instead of silence — are all claims about two
processes, and a fake plan port would satisfy every one of them by
construction. The two that are *about the link being down* would satisfy them
most easily of all, which is why the link is really taken down here rather than
simulated by a port that raises.

**The load-bearing shapes are pinned against hand-written literals.** The method
name and the ``plan.execute`` params are written out below as strings and
numbers rather than derived from :class:`~pz_agent_mcp.remote.methods.Method` or
from the encoder under test: a key spelled wrongly in both halves survives every
round trip, and a params assertion built from ``encode_plan_request`` would
assert that the encoder equals itself.

The server side here is a fixture, not the shipped server half. It exists so the
client can be driven against something that speaks the real envelope over the
real transport.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.protocol import ActionStatus, JsonDict, ReasonCode, SessionMode
from pz_agent_core.rpc.descriptor import runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import (
    DEFAULT_DEADLINE_SECONDS,
    RpcServer,
    new_address,
)
from pz_agent_core.rpc.wire import ErrorCode, RpcRequest, RpcResponse
from pz_agent_mcp.ports import PlanRecord, PlanRequest
from pz_agent_mcp.remote.client import RemotePlanPort
from pz_agent_voice import phrases
from pz_agent_voice.messages import OutputKind, VoiceGoal, VoiceInput, VoiceIntent
from pz_agent_voice.plan_port import (
    MAX_VOICE_PLAN_DEADLINE_SECONDS,
    VOICE_PLAN_DEADLINE_SECONDS,
    core_plan_port,
    services_over_core_rpc,
)
from pz_agent_voice.ports import CoreRefused, SidecarUnavailable, VoiceServices
from pz_agent_voice.session import TOPIC_PLAN, TOPIC_STOP, VoiceSession
from pz_agent_voice.state import VoiceState
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.mcp_doubles import Doubles
from tests.fixtures.voice_doubles import make_session

#: Long enough that a loaded runner does not fail a happy path, short enough
#: that a genuine hang ends one test rather than the suite's patience.
GRACE: Final = 10.0

#: The deadline used by the tests that are *about* the deadline. Short, because
#: waiting the shipped three seconds to prove a timeout is three seconds a run.
IMPATIENT: Final = 0.2

#: What the sidecar answers to ``plan.execute``, written by hand rather than
#: produced by :func:`~pz_agent_mcp.remote.codec.plans.encode_plan_record`. The
#: fields are deliberately not defaults: ``step_index`` is not 0, the status is
#: not the first member of its enum, and there is a step carrying both optional
#: keys, so a decoder that dropped a field and fell back cannot pass.
PINNED_ANSWER: Final[JsonDict] = {
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

#: The params a spoken «поешь» must arrive as. Every value is written out: the
#: goal token, the mode the session snapshot reported, the two bounds
#: :class:`~pz_agent_voice.config.VoiceConfig` chose, and the idempotency key
#: the injected id factory minted. A plan submitted without the bounds is a plan
#: the core runs to a ceiling nobody picked.
PINNED_PARAMS: Final[JsonDict] = {
    "goal": "eat",
    "mode": "ASSISTED",
    "max_steps": 8,
    "max_real_seconds": 120,
    "idempotency_key": "voice-1",
}


# --------------------------------------------------------------------------
# The server side: a real RpcServer on a thread, over a real state directory.
# --------------------------------------------------------------------------

#: What a sidecar does with one request. Returning a response rather than
#: raising keeps a refusal off the exception path, where it would look like a
#: transport fault.
Handler = Callable[[RpcRequest], RpcResponse]


@dataclass
class Sidecar:
    """A running server, its state directory, and what it was asked."""

    state_dir: Path
    server: RpcServer
    thread: threading.Thread
    seen: list[RpcRequest]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.seen]

    @property
    def params(self) -> JsonDict:
        assert len(self.seen) == 1, f"expected exactly one call, saw {self.methods}"
        return self.seen[0].params

    def close(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


Start = Callable[..., Sidecar]


def answering(result: JsonDict) -> Handler:
    """A sidecar that answers *result* to anything."""

    def handler(request: RpcRequest) -> RpcResponse:
        return RpcResponse(id=request.id, ok=True, result=result)

    return handler


def refusing(message: str) -> Handler:
    """A sidecar whose core was asked and said no."""

    def handler(request: RpcRequest) -> RpcResponse:
        return RpcResponse(
            id=request.id, ok=False, error_code=ErrorCode.CORE_REFUSED, error_message=message
        )

    return handler


def stalling(released: threading.Event) -> Handler:
    """A sidecar that accepts the request and then says nothing.

    Parked on an event rather than on a sleep so the test decides when the
    thread is let go, and bounded by :data:`GRACE` so a test that forgets to
    release it leaks one thread for ten seconds instead of forever.
    """

    def handler(request: RpcRequest) -> RpcResponse:
        released.wait(timeout=GRACE)
        return RpcResponse(id=request.id, ok=True, result=dict(PINNED_ANSWER))

    return handler


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Start]:
    """Bring up real servers and take them all down again.

    State directory names are one letter on purpose: a POSIX socket path is
    bounded by ``sun_path``, and a fixture that spelled out the test's name
    would fail to bind under a deep temporary directory instead of testing
    anything.
    """
    started: list[Sidecar] = []

    def _start(*, handler: Handler | None = None) -> Sidecar:
        state = tmp_path / f"s{len(started)}"
        runtime = runtime_dir(state)
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        seen: list[RpcRequest] = []
        answer = handler if handler is not None else answering(dict(PINNED_ANSWER))

        def dispatch(request: RpcRequest) -> RpcResponse:
            seen.append(request)
            return answer(request)

        server = RpcServer(new_address(runtime), authkey=key, handler=dispatch)
        write_descriptor(state, server.descriptor())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sidecar = Sidecar(state_dir=state, server=server, thread=thread, seen=seen)
        started.append(sidecar)
        return sidecar

    yield _start

    for sidecar in started:
        sidecar.close()


def said(text: str, *, at_ms: int = BASE_TIME_MS) -> VoiceInput:
    return VoiceInput(transcript=text, at_ms=at_ms, final=True, confidence=1.0)


def wired(
    sidecar_state: Path, *, deadline: float = VOICE_PLAN_DEADLINE_SECONDS
) -> tuple[VoiceSession, Doubles, VoiceServices]:
    """A companion whose goals take the link and whose stop does not."""
    doubles = Doubles()
    services = services_over_core_rpc(doubles.session, sidecar_state, deadline=deadline)
    session, _, _ = make_session(doubles, services=services)
    return session, doubles, services


def a_request(goal: str = "eat") -> PlanRequest:
    return PlanRequest(
        goal=goal,
        mode=SessionMode.ASSISTED,
        max_steps=4,
        max_real_seconds=60,
        idempotency_key="key-1",
    )


def spoken(session: VoiceSession) -> list[str]:
    return [message.text for message in session.queue.pending]


# --------------------------------------------------------------------------
# T001 — the port reaches the core rather than refusing
# --------------------------------------------------------------------------


def test_a_spoken_goal_arrives_at_the_core_as_plan_execute(start: Start) -> None:
    """The whole of T001: a phrase, a wire, a request the core received."""
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, поешь"))

    assert sidecar.methods == ["plan.execute"]
    assert sidecar.params == PINNED_PARAMS
    assert turn.intent is VoiceIntent.GOAL
    assert turn.goal is VoiceGoal.EAT
    assert spoken(session) == [phrases.GOAL_ACCEPTED[VoiceGoal.EAT]]


def test_the_record_the_core_answered_is_the_one_the_companion_reports(start: Start) -> None:
    """A plan the core is running, not a plan this process invented."""
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, поешь"))

    assert turn.plan is not None
    assert turn.plan.plan_id == "plan-9"
    assert turn.plan.status is ActionStatus.STARTED
    assert turn.plan.step_index == 2
    assert [step.index for step in turn.plan.steps] == [0, 1]
    assert turn.plan.steps[0].reason_code is ReasonCode.POSTCONDITION_MET
    assert turn.plan.steps[0].action_id == "action-4"
    # STARTED is not terminal, so the companion is waiting on this plan rather
    # than treating the submission as the outcome.
    assert session.plan_active is True


def test_current_reads_the_running_plan_over_the_same_link(start: Start) -> None:
    sidecar = start(handler=answering({"plan": dict(PINNED_ANSWER)}))
    port = core_plan_port(sidecar.state_dir)

    record = port.current()

    assert sidecar.methods == ["plan.current"]
    assert record is not None
    assert record.plan_id == "plan-9"


def test_current_answers_none_only_when_the_core_said_there_is_no_plan(start: Start) -> None:
    """``None`` is an answer the core gave, never a link that failed quietly."""
    sidecar = start(handler=answering({}))
    port = core_plan_port(sidecar.state_dir)

    assert port.current() is None

    sidecar.close()
    with pytest.raises(SidecarUnavailable):
        port.current()


# --------------------------------------------------------------------------
# T002 — nothing answers 'not routed'
# --------------------------------------------------------------------------


def test_the_companion_submits_instead_of_refusing_unasked(start: Start) -> None:
    """The placeholder raised without touching a wire. This one cannot.

    The assertion is that the *server* saw the request. A port that refused
    locally would leave ``seen`` empty however plausible its exception was.
    """
    sidecar = start()
    port = core_plan_port(sidecar.state_dir)

    record = port.execute(a_request())

    assert isinstance(record, PlanRecord)
    assert len(sidecar.seen) == 1


def test_a_refusal_is_the_cores_refusal_and_carries_its_words(start: Start) -> None:
    """When a goal is declined, the core declined it — this process never does."""
    sidecar = start(handler=refusing("the planner has no route to that goal"))
    port = core_plan_port(sidecar.state_dir)

    with pytest.raises(CoreRefused) as raised:
        port.execute(a_request())

    assert "the planner has no route to that goal" in str(raised.value)
    assert sidecar.methods == ["plan.execute"]


def test_this_package_ships_no_plan_port_of_its_own(start: Start) -> None:
    """One implementation of ``plan.execute``, and it is the boundary's.

    A voice-shaped copy would be a second spelling of the method name and a
    second path into the engine that the MCP path's tests do not cover.
    """
    sidecar = start()

    assert isinstance(core_plan_port(sidecar.state_dir), RemotePlanPort)


def test_no_source_file_in_this_package_refuses_a_goal_unrouted() -> None:
    """The placeholder is gone from here and must not grow back.

    Scoped to :mod:`pz_agent_voice` because that is the package under test; the
    same names in the CLI's wiring are that module's to remove.
    """
    root = Path(__file__).resolve().parents[2] / "packages" / "pz_agent_voice" / "src"
    offenders = [
        path.name
        for path in sorted(root.rglob("*.py"))
        if "Unrouted" in path.read_text(encoding="utf-8")
        or "GoalUnroutable" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# T003 — arm, disarm and stop stay on their existing short path
# --------------------------------------------------------------------------


def test_the_session_port_handed_in_is_the_one_the_bundle_uses(start: Start) -> None:
    """By identity. A wrapper here is a wrapper on the stop path."""
    sidecar = start()
    doubles = Doubles()

    services = services_over_core_rpc(doubles.session, sidecar.state_dir)

    assert services.session is doubles.session


def test_a_stop_makes_no_call_on_the_goal_channel_even_when_it_is_up(start: Start) -> None:
    """The strongest form of T003: the link is listening and is not dialled."""
    sidecar = start()
    session, doubles, _ = wired(sidecar.state_dir)

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert sidecar.methods == []
    assert turn.stop is not None
    assert spoken(session) == [phrases.STOP_ACK]


def test_arm_and_disarm_make_no_call_on_the_goal_channel(start: Start) -> None:
    sidecar = start()
    _, doubles, services = wired(sidecar.state_dir)

    services.session.arm(SessionMode.ASSISTED, confirm_backup=True)
    services.session.disarm()

    assert doubles.session.arms == [(SessionMode.ASSISTED, True)]
    assert doubles.session.disarms == 1
    assert sidecar.methods == []


# --------------------------------------------------------------------------
# T004 — a stop is honoured with the core UNREACHABLE
# --------------------------------------------------------------------------


def test_a_stop_is_honoured_after_the_core_has_gone(start: Start) -> None:
    """Proved with the link actually down, not with a port that raises."""
    sidecar = start()
    session, doubles, services = wired(sidecar.state_dir)
    assert session.handle(said("агент, поешь")).plan is not None

    sidecar.close()

    # The link really is down: this is what a goal would now hit.
    with pytest.raises(SidecarUnavailable):
        services.plans.execute(a_request())

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert turn.intent is VoiceIntent.STOP
    assert turn.stop is not None
    assert turn.interrupt_speech is True
    assert session.state is VoiceState.IDLE
    assert spoken(session) == [phrases.STOP_ACK]


def test_a_stop_is_honoured_with_no_sidecar_ever_started(tmp_path: Path) -> None:
    """A companion may be started before ``pz-agent start`` and still stop.

    There is no descriptor and no token in this state directory, so the plan
    port has nothing to resolve — and the stop does not care.
    """
    doubles = Doubles()
    services = services_over_core_rpc(doubles.session, tmp_path / "never-started")
    session, _, _ = make_session(doubles, services=services)

    with pytest.raises(SidecarUnavailable):
        services.plans.execute(a_request())

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert turn.stop is not None
    assert spoken(session) == [phrases.STOP_ACK]


def test_a_goal_with_the_core_gone_is_a_spoken_refusal_and_no_plan(start: Start) -> None:
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)
    sidecar.close()

    turn = session.handle(said("агент, поешь"))

    assert turn.plan is None
    assert session.plan_active is False
    assert spoken(session) == [phrases.PLAN_REFUSED]
    assert [message.topic for message in session.queue.pending] == [TOPIC_PLAN]


# --------------------------------------------------------------------------
# T005 — a voice command waits a bounded time, and says so when it expires
# --------------------------------------------------------------------------


def test_a_stalled_core_produces_a_spoken_failure_rather_than_silence(start: Start) -> None:
    """A core that accepts the request and never answers.

    The two assertions that matter are the sentence and the clock. Silence is
    the failure mode a voice interface cannot signal, and a companion that
    waited for this core would produce exactly that.
    """
    released = threading.Event()
    sidecar = start(handler=stalling(released))
    session, _, _ = wired(sidecar.state_dir, deadline=IMPATIENT)
    try:
        began = time.monotonic()
        turn = session.handle(said("агент, поешь"))
        elapsed = time.monotonic() - began
    finally:
        released.set()

    assert sidecar.methods == ["plan.execute"]
    assert turn.plan is None
    assert spoken(session) == [phrases.PLAN_REFUSED]
    assert [message.kind for message in session.queue.pending] == [OutputKind.ERROR]
    # It waited, and it stopped waiting: the lower bound proves the deadline was
    # used rather than the call failing outright, and the upper bound proves the
    # companion did not sit on the core's silence.
    assert IMPATIENT <= elapsed < GRACE


def test_the_wait_is_bounded_by_the_deadline_the_wiring_chose(start: Start) -> None:
    """The bound is the one passed in, not the transport's own default.

    ``GRACE`` is the shipped ten-second default rounded to this file's patience,
    so a port that ignored the deadline would be caught by the upper bound here
    rather than by a test that merely finished eventually.
    """
    released = threading.Event()
    sidecar = start(handler=stalling(released))
    port = core_plan_port(sidecar.state_dir, deadline=IMPATIENT)
    try:
        began = time.monotonic()
        with pytest.raises(SidecarUnavailable) as raised:
            port.execute(a_request())
        elapsed = time.monotonic() - began
    finally:
        released.set()

    assert "no answer within" in str(raised.value)
    assert IMPATIENT <= elapsed < IMPATIENT * 10


def test_the_default_deadline_is_short_and_never_longer_than_the_mcp_path() -> None:
    """Written as literals, and cross-checked against the transport's own.

    The ceiling is declared in :mod:`pz_agent_voice.plan_port` as its own number
    rather than borrowed, so this is what catches the two of them drifting apart
    — a transport default that grew would otherwise drag a spoken turn out with
    it and nothing would say so.
    """
    assert VOICE_PLAN_DEADLINE_SECONDS == 3.0
    assert MAX_VOICE_PLAN_DEADLINE_SECONDS == 10.0
    assert MAX_VOICE_PLAN_DEADLINE_SECONDS == DEFAULT_DEADLINE_SECONDS
    assert VOICE_PLAN_DEADLINE_SECONDS < MAX_VOICE_PLAN_DEADLINE_SECONDS


@pytest.mark.parametrize("deadline", [0.0, -1.0, 10.5, 60.0])
def test_a_deadline_outside_the_bound_is_refused_at_wiring_time(
    tmp_path: Path, deadline: float
) -> None:
    """A wiring mistake that only surfaces when somebody speaks is one that ships."""
    with pytest.raises(ValueError, match="at most"):
        core_plan_port(tmp_path, deadline=deadline)

    with pytest.raises(ValueError, match="at most"):
        services_over_core_rpc(Doubles().session, tmp_path, deadline=deadline)


def test_the_ceiling_itself_is_accepted(tmp_path: Path) -> None:
    """The bound is inclusive; only past it is refused."""
    assert core_plan_port(tmp_path, deadline=MAX_VOICE_PLAN_DEADLINE_SECONDS) is not None


def test_a_stop_still_works_while_the_core_is_stalled(start: Start) -> None:
    """The goal channel is wedged mid-call and the stop lever is unaffected.

    The closest thing to the real failure this milestone is about: not a link
    that is down, but one that has accepted a request and gone quiet, which is
    the state in which a shared transport would have the stop queued behind it.
    """
    released = threading.Event()
    sidecar = start(handler=stalling(released))
    session, doubles, _ = wired(sidecar.state_dir, deadline=IMPATIENT)
    try:
        session.handle(said("агент, поешь"))

        turn = session.handle(said("стоп"))
    finally:
        released.set()

    assert doubles.session.stops == 1
    assert turn.stop is not None
    assert [message.topic for message in session.queue.pending] == [TOPIC_STOP]
    assert spoken(session) == [phrases.STOP_ACK]
