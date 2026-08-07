"""The companion's goal port, against a real core over a real Core RPC link.

Not a double. Every test that claims a goal reaches the core starts an
:class:`~pz_agent_core.rpc.transport.RpcServer` on a thread, writes a real
descriptor and a real token, and lets
:func:`~pz_agent_voice.plan_port.services_over_core_rpc` find its way there the
way ``pz-agent voice run`` will. The claims under test — a goal is submitted
rather than refused unasked, a stop is honoured with the core unreachable, a
wedged core produces a sentence instead of silence — are all claims about two
processes, and a fake goal port would satisfy every one of them by construction.
The two that are *about the link being down* would satisfy them most easily of
all, which is why the link is really taken down here rather than simulated by a
port that raises.

``goal.submit``, not ``plan.execute``
-------------------------------------

``docs/control/DECISIONS.md`` § "The voice companion routes through
``goal.submit``, not ``plan.execute``" is what these tests hold the wiring to.
The method name asserted below is ``goal.submit``; a spoken quantity is asserted
to arrive as a typed ``params`` field; and ``resume`` is asserted to reach
``goal.status`` and *never* ``goal.submit``, because it names no work and the
decision refuses to invent a fifth
:class:`~pz_agent_core.goals.GoalKind` for it.

**The load-bearing shapes are pinned against hand-written literals.** The method
names and the ``goal.submit`` params are written out below as strings and numbers
rather than derived from :class:`~pz_agent_mcp.remote.methods.Method` or from the
encoder under test: a key spelled wrongly in both halves survives every round
trip, and a params assertion built from ``encode_goal_request`` would assert that
the encoder equals itself. The spoken sentences are written out for the same
reason — comparing ``phrases.X`` against ``phrases.X`` is a tautology.

**The privacy claim is asserted over bytes.** ``no transcript text reaches the
core`` is a statement about what went down the wire, and an assertion over the
decoded object's attributes cannot see a field that carried the words somewhere
else. :func:`wire_bytes` re-encodes the request the server actually received with
the same :func:`~pz_agent_core.rpc.wire.encode_request` the client used, which
reproduces the transmitted bytes exactly, and the assertions are ``in`` /
``not in`` over that.

The server side here is a fixture, not the shipped server half. It exists so the
client can be driven against something that speaks the real envelope over the
real transport.
"""

from __future__ import annotations

# The sentences pinned below are the ones this build says out loud, short enough
# that every letter in them has a Latin lookalike — which the confusable-character
# rule reads as mistyped ASCII. They are not mistyped: taking the rule's
# suggestion would leave a literal that no phrase in the package equals, and the
# assertion would fail for a reason that has nothing to do with the wiring.
# ruff: noqa: RUF001
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.goals import GoalKind, GoalParams, GoalRequest
from pz_agent_core.protocol import JsonDict, SessionMode
from pz_agent_core.rpc.descriptor import runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import (
    DEFAULT_DEADLINE_SECONDS,
    RpcServer,
    new_address,
)
from pz_agent_core.rpc.wire import ErrorCode, RpcRequest, RpcResponse, encode_request
from pz_agent_mcp.remote.client import RemoteGoalPort, RemotePlanPort
from pz_agent_voice.messages import OutputKind, VoiceGoal, VoiceInput, VoiceIntent
from pz_agent_voice.plan_port import (
    MAX_VOICE_PLAN_DEADLINE_SECONDS,
    VOICE_PLAN_DEADLINE_SECONDS,
    core_goal_port,
    core_plan_port,
    services_over_core_rpc,
)
from pz_agent_voice.ports import CoreRefused, SidecarUnavailable, VoiceServices
from pz_agent_voice.session import TOPIC_PLAN, TOPIC_STOP, VoiceSession
from pz_agent_voice.state import VoiceState
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.mcp_doubles import Doubles
from tests.fixtures.voice_doubles import HOSTILE_TRANSCRIPT, make_session

#: Long enough that a loaded runner does not fail a happy path, short enough
#: that a genuine hang ends one test rather than the suite's patience.
GRACE: Final = 10.0

#: The deadline used by the tests that are *about* the deadline. Short, because
#: waiting the shipped three seconds to prove a timeout is three seconds a run.
IMPATIENT: Final = 0.2

#: Sentences this build speaks, written out rather than imported from
#: :mod:`pz_agent_voice.phrases`. Comparing the module against itself would pass
#: however the table was edited.
STOP_ACK: Final = "Остановился."
GOAL_REFUSED: Final = "Не получилось."
EATING: Final = "Ищу, что съесть."
DRINKING: Final = "Ищу, что выпить."
READING: Final = "Ищу, что почитать."
CONTINUING: Final = "Продолжаю."
NO_SUCH_BUILD: Final = "Эта команда недоступна в этой сборке игры."
PAGES_OUT_OF_RANGE: Final = "Число страниц — от 1 до 200. Назови число в этих пределах."
LEVEL_NOT_ACCEPTED: Final = "Здесь уровень не задаётся. Повтори без этого."

#: A goal record as a sidecar answers one, written by hand rather than produced
#: by :func:`~pz_agent_mcp.remote.codec.goals.encode_goal_record`. The fields are
#: deliberately not defaults: the sequence is not 0, the params are not empty,
#: and the budget is not the kind's, so a decoder that dropped a field and fell
#: back on a default cannot pass.
PINNED_GOAL: Final[JsonDict] = {
    "goal_id": "goal-7",
    "kind": "satisfy_hunger",
    "params": {"satisfy_to": 0.8},
    "budget": {"max_wall_ms": 90_000, "max_steps": 3, "pending_ttl_ms": 45_000},
    "key_digest": "0123456789abcdef",
    "sequence": 4,
    "state": "pending",
    "submitted_at_ms": 1_700_000_000_000,
    "started_at_ms": None,
    "finished_at_ms": None,
    "steps_used": 0,
    "reason_code": None,
    "evidence_keys": [],
    "detail": "",
}

#: The same goal, running. Used by the ``resume`` tests, which are about a goal
#: the channel already holds.
PINNED_ACTIVE_GOAL: Final[JsonDict] = {
    **PINNED_GOAL,
    "state": "active",
    "started_at_ms": 1_700_000_000_500,
}

#: What the sidecar answers to ``goal.submit``.
PINNED_ADMISSION: Final[JsonDict] = {"duplicate": False, "goal": PINNED_GOAL}

#: What it answers to ``goal.status`` when a goal is running.
PINNED_CHANNEL: Final[JsonDict] = {
    "active": PINNED_ACTIVE_GOAL,
    "pending": [],
    "named": None,
}

#: And when the channel is empty. ``pending`` is an empty array rather than a
#: missing key: an empty backlog is a fact, and a dropped key is a truncated
#: body.
EMPTY_CHANNEL: Final[JsonDict] = {"active": None, "pending": [], "named": None}

#: The params a spoken «поешь» must arrive as. Every value is written out: the
#: kind the decision record maps ``eat`` onto, the idempotency key the injected
#: id factory minted, an empty parameter object because the phrase named no
#: quantity, and the three bounds :class:`~pz_agent_voice.config.VoiceConfig`
#: chose. A goal submitted without the budget is a goal the core runs to a
#: ceiling nobody picked.
EAT_BUDGET: Final[JsonDict] = {
    "max_wall_ms": 120_000,
    "max_steps": 8,
    "pending_ttl_ms": 60_000,
}
READ_BUDGET: Final[JsonDict] = {
    "max_wall_ms": 120_000,
    "max_steps": 8,
    "pending_ttl_ms": 120_000,
}
PINNED_SUBMIT: Final[JsonDict] = {
    "kind": "satisfy_hunger",
    "idempotency_key": "voice-1",
    "params": {},
    "budget": EAT_BUDGET,
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


def wire_bytes(sidecar: Sidecar) -> bytes:
    """The bytes the client put on the wire for its one call.

    Re-encoded with the shipped :func:`~pz_agent_core.rpc.wire.encode_request`,
    which is the function that produced them: the envelope is compact JSON with
    no key sorting, so decoding and re-encoding reproduces the transmitted bytes
    rather than a re-rendering of them. Asserting over these is the only way to
    catch a transcript that travelled in a field nobody thought to read back.
    """
    assert len(sidecar.seen) == 1, f"expected exactly one call, saw {sidecar.methods}"
    return encode_request(sidecar.seen[0])


def answering(result: JsonDict) -> Handler:
    """A sidecar that answers *result* to anything."""

    def handler(request: RpcRequest) -> RpcResponse:
        return RpcResponse(id=request.id, ok=True, result=result)

    return handler


def routing(answers: Mapping[str, JsonDict]) -> Handler:
    """A sidecar that answers per method, and refuses the rest by name."""

    def handler(request: RpcRequest) -> RpcResponse:
        result = answers.get(request.method)
        if result is None:
            return RpcResponse(
                id=request.id,
                ok=False,
                error_code=ErrorCode.UNKNOWN_METHOD,
                error_message=f"this fixture answers {sorted(answers)}",
            )
        return RpcResponse(id=request.id, ok=True, result=dict(result))

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
        return RpcResponse(id=request.id, ok=True, result=dict(PINNED_ADMISSION))

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
        answer = handler if handler is not None else answering(dict(PINNED_ADMISSION))

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


def a_request(kind: GoalKind = GoalKind.SATISFY_HUNGER) -> GoalRequest:
    return GoalRequest(kind=kind, idempotency_key="key-1", params=GoalParams())


def spoken(session: VoiceSession) -> list[str]:
    return [message.text for message in session.queue.pending]


# --------------------------------------------------------------------------
# E09-M01-T001 — the port reaches the core rather than refusing
# --------------------------------------------------------------------------


def test_a_spoken_goal_arrives_at_the_core_as_goal_submit(start: Start) -> None:
    """The whole of T001: a phrase, a wire, a request the core received."""
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, поешь"))

    assert sidecar.methods == ["goal.submit"]
    assert sidecar.params == PINNED_SUBMIT
    assert turn.intent is VoiceIntent.GOAL
    assert turn.goal is VoiceGoal.EAT
    assert spoken(session) == [EATING]


def test_the_goal_the_core_admitted_is_the_one_the_companion_reports(start: Start) -> None:
    """A goal the channel is holding, not a goal this process invented."""
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, поешь"))

    # The record's own id and state, read back out of the answer and reported.
    assert "goal-7" in turn.detail
    assert "pending" in turn.detail
    # PENDING is not terminal, so the companion is waiting on this goal rather
    # than treating the submission as the outcome.
    assert session.plan_active is True


@pytest.mark.parametrize(
    ("phrase", "kind", "confirmation", "budget"),
    [
        ("агент, поешь", "satisfy_hunger", EATING, EAT_BUDGET),
        ("агент, попей", "satisfy_thirst", DRINKING, EAT_BUDGET),
        ("агент, почитай", "read_for_boredom", READING, READ_BUDGET),
    ],
)
def test_each_spoken_goal_maps_onto_the_kind_the_decision_record_names(
    start: Start, phrase: str, kind: str, confirmation: str, budget: JsonDict
) -> None:
    """The mapping table, asserted one row at a time against literals.

    Every column is hand-written: the kind's wire value, the sentence, and the
    budget. A table derived from :data:`~pz_agent_voice.session._GOAL_KIND` would
    agree with any edit to it.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said(phrase))

    assert sidecar.methods == ["goal.submit"]
    assert sidecar.params["kind"] == kind
    assert sidecar.params["budget"] == budget
    assert spoken(session) == [confirmation]


def test_the_companion_submits_instead_of_refusing_unasked(start: Start) -> None:
    """The placeholder raised without touching a wire. This one cannot.

    The assertion is that the *server* saw the request. A port that refused
    locally would leave ``seen`` empty however plausible its exception was.
    """
    sidecar = start()
    port = core_goal_port(sidecar.state_dir)

    admission = port.submit(a_request())

    assert admission.accepted is True
    assert sidecar.methods == ["goal.submit"]


def test_a_refusal_is_the_cores_refusal_and_carries_its_words(start: Start) -> None:
    """When the core declines outright, it declined — this process never does."""
    sidecar = start(handler=refusing("the goal channel is not accepting submissions"))
    port = core_goal_port(sidecar.state_dir)

    with pytest.raises(CoreRefused) as raised:
        port.submit(a_request())

    assert "the goal channel is not accepting submissions" in str(raised.value)
    assert sidecar.methods == ["goal.submit"]


def test_this_package_ships_no_goal_port_of_its_own(start: Start) -> None:
    """One implementation of ``goal.submit``, and it is the boundary's.

    A voice-shaped copy would be a second spelling of the method name and a
    second path into the engine that the MCP path's tests do not cover.
    """
    sidecar = start()

    assert isinstance(core_goal_port(sidecar.state_dir), RemoteGoalPort)
    assert isinstance(core_plan_port(sidecar.state_dir), RemotePlanPort)


def test_no_source_file_in_this_package_refuses_a_goal_unrouted() -> None:
    """The placeholder is gone from here and must not grow back.

    Scoped to :mod:`pz_agent_voice` because that is the package under test; the
    same names in the CLI's wiring are that module's to remove.

    An empty ``offenders`` proves nothing on its own: a root that stopped
    resolving — this file moved, the package moved — makes :meth:`Path.rglob`
    yield nothing and the assertion below pass without reading a line. So the
    scan is required to have found the module it is scanning *for* first.
    """
    root = Path(__file__).resolve().parents[2] / "packages" / "pz_agent_voice" / "src"
    scanned = {path: path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))}

    assert root / "pz_agent_voice" / "plan_port.py" in scanned, f"scanned nothing under {root}"

    offenders = [
        path.name
        for path, source in scanned.items()
        if "Unrouted" in source or "GoalUnroutable" in source
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# No transcript text crosses, asserted over the bytes
# --------------------------------------------------------------------------


def test_no_transcript_text_reaches_the_core_in_the_bytes_it_sent(start: Start) -> None:
    """A hostile transcript, and the whole request it produced, as bytes.

    The transcript carries an instruction, a line break, a mode name and a
    Windows path, and it also carries a real command word so the classifier still
    finds the goal in it — which is the only way to prove the *rest* of it went
    nowhere. The assertions are over what was transmitted, not over the decoded
    object: a field that smuggled the words would still be there in the bytes.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said(HOSTILE_TRANSCRIPT))

    raw = wire_bytes(sidecar)
    lowered = raw.lower()
    for fragment in (
        "SYSTEM",
        "ignore previous instructions",
        "AUTONOMOUS",
        "secrets",
        "hostile",
        "Users",
        "поешь",
        "агент",
    ):
        # Folded on both sides: the matcher case-folds a transcript before it
        # reads it, so a leak would arrive in lower case and a case-sensitive
        # search would miss it.
        assert fragment.lower().encode("utf-8") not in lowered, f"{fragment!r} reached the core"
    assert b"\\n" not in raw
    # And what *did* travel is exactly the closed vocabulary.
    assert json.loads(raw.decode("utf-8"))["params"] == PINNED_SUBMIT


def test_the_request_body_carries_no_field_the_pinned_shape_does_not(start: Start) -> None:
    """A whole-object comparison, so a new free-text field fails here.

    Asserting key by key would let an added ``transcript`` or ``phrase`` key
    through; equality against a hand-written literal cannot.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said("агент, поешь"))

    body = json.loads(wire_bytes(sidecar).decode("utf-8"))
    assert body["method"] == "goal.submit"
    assert body["params"] == PINNED_SUBMIT


# --------------------------------------------------------------------------
# E09-M02-T003/T004 — a quantity becomes a typed field, out of range is refused
# --------------------------------------------------------------------------


def test_a_spoken_quantity_arrives_as_a_typed_field(start: Start) -> None:
    """T003: "почитай 12 страниц" reaches the core as ``pages=12``.

    Asserted in the bytes as well as in the decoded params, because the point of
    the typed field is that the number crossed as a number rather than inside a
    goal string.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said("агент, почитай 12 страниц"))

    raw = wire_bytes(sidecar)
    # A number, not a substring of a goal string: the digits are there, and the
    # unit word the user said them with is not.
    assert b'"pages":12' in raw
    assert "страниц".encode() not in raw
    assert json.loads(raw.decode("utf-8"))["params"] == {
        "kind": "read_for_boredom",
        "idempotency_key": "voice-1",
        "params": {"pages": 12},
        "budget": READ_BUDGET,
    }


def test_a_percentage_is_converted_into_the_cores_own_unit(start: Start) -> None:
    """Speech says eighty per cent; ``satisfy_to`` is a fraction of one.

    A companion that forwarded 80 would be refused by the range it never
    converted for, and the user would be told the number they said was too big.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said("агент, поешь на 80 процентов"))

    assert sidecar.params["params"] == {"satisfy_to": 0.8}
    assert b'"satisfy_to":0.8' in wire_bytes(sidecar)


def test_a_quantity_outside_its_range_is_refused_before_the_wire(start: Start) -> None:
    """T004: 999 pages is refused, and the refusal names the bound.

    Nothing is dialled. A goal the channel would reject is not worth a round
    trip, and refusing locally is what lets the sentence name the range rather
    than relay whatever the core said about it.
    """
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, почитай 999 страниц"))

    assert sidecar.methods == []
    assert spoken(session) == [PAGES_OUT_OF_RANGE]
    assert [message.kind for message in session.queue.pending] == [OutputKind.ERROR]
    assert "999" not in turn.detail
    assert "999" not in spoken(session)[0]


def test_a_quantity_the_kind_does_not_take_is_refused_by_name(start: Start) -> None:
    """A level named on a request to eat is refused, not silently dropped."""
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said("агент, поешь до пятого уровня"))

    assert sidecar.methods == []
    assert spoken(session) == [LEVEL_NOT_ACCEPTED]


# --------------------------------------------------------------------------
# A refusal from the channel is spoken, and never echoes the transcript
# --------------------------------------------------------------------------


def test_a_goal_the_channel_refuses_is_spoken_about_and_not_echoed(start: Start) -> None:
    """The channel answers with a refusal rather than raising.

    ``QUEUE_REJECTED`` is what the queue answers when its cap is reached and when
    a goal is already active. The companion says something about it — silence is
    the failure a voice interface cannot signal — and the turn's detail carries
    the reason code and the active goal's id, both of which the core minted. The
    core's own English sentence stays out of the loudspeaker.
    """
    refusal = {
        "reason_code": "QUEUE_REJECTED",
        "message": "goal goal-3 (satisfy_thirst) is already active; cancel it or wait.",
        "active_goal_id": "goal-3",
    }
    sidecar = start(handler=answering({"duplicate": False, "refusal": refusal}))
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, поешь"))

    assert sidecar.methods == ["goal.submit"]
    assert spoken(session) == [GOAL_REFUSED]
    assert [message.kind for message in session.queue.pending] == [OutputKind.ERROR]
    assert session.plan_active is False
    assert "QUEUE_REJECTED" in turn.detail
    assert "goal-3" in turn.detail
    assert "already active" not in spoken(session)[0]


def test_a_companion_with_no_goal_channel_says_so_and_dials_nothing(start: Start) -> None:
    """The bundle's ``goals`` may be ``None``, and then a goal is answered honestly."""
    sidecar = start()
    doubles = Doubles()
    services = VoiceServices(session=doubles.session, plans=doubles.plans)
    session, _, _ = make_session(doubles, services=services)

    session.handle(said("агент, поешь"))

    assert sidecar.methods == []
    assert doubles.plans.requests == []
    assert spoken(session) == [NO_SUCH_BUILD]


def test_nothing_in_this_package_submits_a_plan_for_a_spoken_goal(start: Start) -> None:
    """The decision record's other half: ``plan.execute`` is not the route.

    The plan port is present and working — the fake counts what it is asked — and
    a spoken goal must not reach it.
    """
    sidecar = start()
    doubles = Doubles()
    services = VoiceServices(
        session=doubles.session,
        plans=doubles.plans,
        goals=core_goal_port(sidecar.state_dir),
    )
    session, _, _ = make_session(doubles, services=services)

    session.handle(said("агент, поешь"))

    assert doubles.plans.requests == []
    assert sidecar.methods == ["goal.submit"]


# --------------------------------------------------------------------------
# resume is a control verb, served from goal.status
# --------------------------------------------------------------------------


def test_resume_reads_the_channel_and_never_submits_a_fifth_kind(start: Start) -> None:
    sidecar = start(handler=routing({"goal.status": PINNED_CHANNEL}))
    session, _, _ = wired(sidecar.state_dir)

    turn = session.handle(said("агент, продолжай"))

    assert sidecar.methods == ["goal.status"]
    assert turn.goal is VoiceGoal.RESUME
    assert spoken(session) == [CONTINUING]
    assert "goal-7" in turn.detail


def test_resume_with_nothing_open_does_not_claim_to_be_continuing(start: Start) -> None:
    """There is nothing to continue, so "Продолжаю." would be a fabricated success."""
    sidecar = start(handler=routing({"goal.status": EMPTY_CHANNEL}))
    session, _, _ = wired(sidecar.state_dir)

    session.handle(said("агент, продолжай"))

    assert sidecar.methods == ["goal.status"]
    assert spoken(session) == [GOAL_REFUSED]
    assert [message.kind for message in session.queue.pending] == [OutputKind.ERROR]


def test_resume_with_the_core_gone_is_a_spoken_failure(start: Start) -> None:
    sidecar = start(handler=routing({"goal.status": PINNED_CHANNEL}))
    session, _, _ = wired(sidecar.state_dir)
    sidecar.close()

    session.handle(said("агент, продолжай"))

    assert spoken(session) == [GOAL_REFUSED]


# --------------------------------------------------------------------------
# E09-M01-T003 — arm, disarm and stop stay on their existing short path
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
    assert spoken(session) == [STOP_ACK]


def test_arm_and_disarm_make_no_call_on_the_goal_channel(start: Start) -> None:
    sidecar = start()
    _, doubles, services = wired(sidecar.state_dir)

    services.session.arm(SessionMode.ASSISTED, confirm_backup=True)
    services.session.disarm()

    assert doubles.session.arms == [(SessionMode.ASSISTED, True)]
    assert doubles.session.disarms == 1
    assert sidecar.methods == []


# --------------------------------------------------------------------------
# E09-M01-T004 — a stop is honoured with the core UNREACHABLE
# --------------------------------------------------------------------------


def test_a_stop_is_honoured_after_the_core_has_gone(start: Start) -> None:
    """Proved with the link actually down, not with a port that raises."""
    sidecar = start()
    session, doubles, services = wired(sidecar.state_dir)
    assert session.handle(said("агент, поешь")).intent is VoiceIntent.GOAL
    assert sidecar.methods == ["goal.submit"]

    sidecar.close()

    # The link really is down: this is what a goal would now hit.
    assert services.goals is not None
    with pytest.raises(SidecarUnavailable):
        services.goals.submit(a_request())

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert turn.intent is VoiceIntent.STOP
    assert turn.stop is not None
    assert turn.interrupt_speech is True
    assert session.state is VoiceState.IDLE
    assert spoken(session) == [STOP_ACK]


def test_a_stop_is_honoured_with_no_sidecar_ever_started(tmp_path: Path) -> None:
    """A companion may be started before ``pz-agent start`` and still stop.

    There is no descriptor and no token in this state directory, so the goal
    port has nothing to resolve — and the stop does not care.
    """
    doubles = Doubles()
    services = services_over_core_rpc(doubles.session, tmp_path / "never-started")
    session, _, _ = make_session(doubles, services=services)

    assert services.goals is not None
    with pytest.raises(SidecarUnavailable):
        services.goals.submit(a_request())

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert turn.stop is not None
    assert spoken(session) == [STOP_ACK]


def test_a_goal_with_the_core_gone_is_a_spoken_refusal_and_no_goal(start: Start) -> None:
    sidecar = start()
    session, _, _ = wired(sidecar.state_dir)
    sidecar.close()

    turn = session.handle(said("агент, поешь"))

    assert session.plan_active is False
    assert "SidecarUnavailable" in turn.detail
    assert spoken(session) == [GOAL_REFUSED]
    assert [message.topic for message in session.queue.pending] == [TOPIC_PLAN]


# --------------------------------------------------------------------------
# E09-M01-T005 — a voice command waits a bounded time, and says so when it expires
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

    assert sidecar.methods == ["goal.submit"]
    assert session.plan_active is False
    assert "SidecarUnavailable" in turn.detail
    assert spoken(session) == [GOAL_REFUSED]
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
    port = core_goal_port(sidecar.state_dir, deadline=IMPATIENT)
    try:
        began = time.monotonic()
        with pytest.raises(SidecarUnavailable) as raised:
            port.submit(a_request())
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
        core_goal_port(tmp_path, deadline=deadline)

    with pytest.raises(ValueError, match="at most"):
        core_plan_port(tmp_path, deadline=deadline)

    with pytest.raises(ValueError, match="at most"):
        services_over_core_rpc(Doubles().session, tmp_path, deadline=deadline)


def test_the_ceiling_itself_is_accepted(tmp_path: Path) -> None:
    """The bound is inclusive; only past it is refused."""
    assert core_goal_port(tmp_path, deadline=MAX_VOICE_PLAN_DEADLINE_SECONDS) is not None


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
    assert spoken(session) == [STOP_ACK]
