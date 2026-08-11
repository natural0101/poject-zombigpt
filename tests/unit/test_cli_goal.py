"""``pz-agent goal``, driven through the real parser against a real core.

The command has one job that cannot be faked: putting a goal into another
process. So the tests that matter here run the actual ``main`` from an actual
argument vector against an actual
:class:`~pz_agent_core.rpc.transport.RpcServer` on an actual socket, with a real
descriptor and a real token — the harness ``tests/unit/test_cli_voice.py``
built for the same reason. Every cheaper double answers the same way whether or
not a wire exists, and "the goal reached the sidecar" is precisely the claim
this command makes.

What the core answers is written out by hand rather than produced by the
encoders. A body built with ``encode_goal_record`` would assert that the codec
equals itself; a key misspelled in both halves would survive it. Two of those
hand-written bodies matter more than the rest:

* ``a_goal_body`` carries the suspension fields, because a CLI built against a
  codec that dropped them would print a goal parked mid-mission as one waiting
  its turn;
* ``OLD_PEER_STATUS`` carries none of the channel's three tails, which is what a
  sidecar built before them answers — and the assertion on it is that the
  command prints ``unreported`` and never ``no``, ``0`` or ``false``. That
  distinction is the whole reason the word exists: "the agent is not paused" and
  "nobody told me whether it is" are different sentences, and a user acts on
  them differently.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeAlias

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from pz_agent_cli.goal_cli import (
    MAX_GOAL_MINUTES,
    NO_SIDECAR_REMEDY,
    UNREPORTED,
    build_request,
)
from pz_agent_core.goals import GOAL_SPECS, GoalKind, GoalParams, TrainableSkill
from pz_agent_core.protocol import JsonDict
from pz_agent_core.rpc.descriptor import runtime_dir, write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from tests.fixtures.cli_worlds import CliWorld, make_world

#: Long enough that a loaded runner does not fail a happy path, short enough
#: that a genuine hang ends one test rather than the suite's patience.
GRACE: Final = 10.0

#: A budget written out by hand and equal to no entry in ``GOAL_SPECS``, so an
#: assertion about a budget cannot pass by the decoder resolving a default.
BUDGET: Final[JsonDict] = {"max_wall_ms": 45_000, "max_steps": 3, "pending_ttl_ms": 9_000}


def a_goal_body(
    *,
    goal_id: str = "goal-1",
    kind: str = "satisfy_hunger",
    state: str = "pending",
    params: JsonDict | None = None,
    **extra: Any,
) -> JsonDict:
    """One goal record as a core answers it, keys written out by hand."""
    body: JsonDict = {
        "goal_id": goal_id,
        "kind": kind,
        "params": {} if params is None else params,
        "budget": dict(BUDGET),
        "key_digest": "0123456789abcdef",
        "sequence": 1,
        "state": state,
        "submitted_at_ms": 1_000,
        "started_at_ms": None,
        "finished_at_ms": None,
        "steps_used": 0,
        "reason_code": None,
        "evidence_keys": [],
        "detail": "",
    }
    body.update(extra)
    return body


#: A channel answering the way a sidecar built before the three tails does: the
#: three goal slots and nothing else. The rendering of *this* is what the
#: "unreported" tests are about.
OLD_PEER_STATUS: Final[JsonDict] = {"active": None, "named": None, "pending": []}


def a_status(**fields: Any) -> JsonDict:
    return {**OLD_PEER_STATUS, **fields}


# ---------------------------------------------------------------------------
# a real core on a real socket
# ---------------------------------------------------------------------------

#: What a core answers for one method: a fixed body, or a function of the
#: request. The second exists for ``goal.cancel``, which must answer about the
#: id it was given if the ``--all`` loop is to be tested at all.
Answer: TypeAlias = "JsonDict | Callable[[RpcRequest], JsonDict]"


@dataclass
class Core:
    """A real RPC server on a real socket, and what it was asked."""

    state_dir: Path
    server: RpcServer
    thread: threading.Thread
    seen: list[RpcRequest]
    answers: dict[str, Answer]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.seen]

    def params_of(self, method: str) -> JsonDict:
        """The params of the first call to *method*, as the server received them."""
        for request in self.seen:
            if request.method == method:
                return dict(request.params)
        raise AssertionError(f"the command never called {method}")

    def close(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


#: The fixture below hands one of these out.
Start: TypeAlias = "Callable[[Path], Core]"


@pytest.fixture
def start_core() -> Iterator[Start]:
    """Bring up real cores and take them all down again.

    The state directory is given by the caller and kept short: a POSIX socket
    path is bounded by ``sun_path``, and the profile directory
    :func:`~tests.fixtures.cli_worlds.make_world` hands out is far too deep to
    bind under.
    """
    started: list[Core] = []

    def _start(state_dir: Path) -> Core:
        runtime = runtime_dir(state_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        key = issue_token(runtime)
        seen: list[RpcRequest] = []
        answers: dict[str, Answer] = {}

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
            body = answer(request) if callable(answer) else dict(answer)
            return RpcResponse(id=request.id, ok=True, result=body)

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


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A whole fake machine whose state directory is short enough to bind in."""
    built = make_world(tmp_path)
    built.ctx = built.ctx.with_overrides(state_dir=tmp_path / "s")
    return built


def document(world: CliWorld) -> JsonDict:
    """The one JSON document a ``--json`` run printed."""
    loaded = json.loads(world.stdout)
    assert isinstance(loaded, dict)
    return loaded


# ---------------------------------------------------------------------------
# the group itself
# ---------------------------------------------------------------------------


def test_the_bare_command_prints_the_group_help_and_exits_usage(world: CliWorld) -> None:
    """A group with no subcommand is a usage error, and the help is the answer.

    Printing the help rather than a sentence about it: the user is one word
    away from what they wanted, and the three words they may choose from are in
    the output they already have.
    """
    code = world.run("goal")

    assert code == EXIT_USAGE
    for subcommand in ("submit", "status", "cancel"):
        assert subcommand in world.stdout
    # The rule the module docstring states, where a user meets it.
    assert "pause" in world.stdout


def test_the_group_says_there_is_no_pause_verb(world: CliWorld) -> None:
    """The one place a user looking for ``pause`` will look.

    ``methods.py`` has three goal verbs by design — manual input already
    outranks the agent and suspension belongs to the arbiter — so the command
    that would exist to pause a goal must say why it does not rather than let a
    user hunt for it.
    """
    world.run("goal")

    assert "arbiter" in world.stdout
    assert "cancel" in world.stdout


# ---------------------------------------------------------------------------
# submit: what is refused before anything is dialled
# ---------------------------------------------------------------------------


def test_an_unknown_kind_is_refused_with_the_whole_list(world: CliWorld, start_core: Start) -> None:
    """Refused here, and refused *before* the link is dialled.

    Both halves matter. The list is what a user needs; not dialling is what
    keeps a mistyped kind from being reported as an unreachable sidecar on a
    machine where the sidecar is fine.
    """
    core = start_core(world.state_dir)

    code = world.run("goal", "submit", "eat_the_dog")

    assert code == EXIT_USAGE
    assert core.methods == [], "a mistyped kind reached the core"
    for kind in GoalKind:
        assert kind.value in world.stderr


def test_an_unknown_parameter_is_refused_with_the_whole_list(world: CliWorld) -> None:
    code = world.run("goal", "submit", "train_skill", "--param", "skil=carpentry")

    assert code == EXIT_USAGE
    assert "skil" in world.stderr
    for name in ("skill", "target_level", "target_endurance", "hours"):
        assert name in world.stderr


def test_a_parameter_the_kind_does_not_take_is_refused_by_the_channel_s_own_rule(
    world: CliWorld,
) -> None:
    """``GOAL_SPECS`` decides, and its sentence is relayed rather than rewritten."""
    code = world.run("goal", "submit", "satisfy_hunger", "--param", "pages=6")

    assert code == EXIT_USAGE
    assert "satisfy_hunger" in world.stderr
    assert "pages" in world.stderr


def test_a_required_parameter_left_out_is_refused(world: CliWorld) -> None:
    code = world.run("goal", "submit", "rest_until")

    assert code == EXIT_USAGE
    assert "target_endurance" in world.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ("goal", "submit", "train_skill", "--param", "skill=swordsmanship"),
        ("goal", "submit", "train_skill", "--param", "skill=carpentry", "--param", "pages=lots"),
        ("goal", "submit", "rest_until", "--param", "target_endurance=1.5"),
        ("goal", "submit", "rest_until", "--param", "target_endurance=nan"),
        ("goal", "submit", "loot_area", "--param", "take_all=maybe"),
        ("goal", "submit", "loot_area", "--param", "scope=everywhere"),
        ("goal", "submit", "satisfy_hunger", "--param", "satisfy_to"),
        ("goal", "submit", "read_for_boredom", "--param", "pages=1", "--param", "pages=2"),
    ],
    ids=[
        "unknown-skill",
        "not-a-number",
        "out-of-range",
        "nan-lies-outside-every-range",
        "not-a-flag",
        "unknown-scope",
        "no-equals-sign",
        "repeated-parameter",
    ],
)
def test_every_way_a_parameter_is_not_one_is_a_usage_error(
    world: CliWorld, argv: tuple[str, ...]
) -> None:
    """None of these reaches a socket, and none of them is a traceback."""
    assert world.run(*argv) == EXIT_USAGE
    assert world.stderr.strip(), "a refusal that says nothing is not a refusal"


def test_minutes_outside_the_channel_s_ceiling_is_refused_with_the_bound(
    world: CliWorld,
) -> None:
    code = world.run("goal", "submit", "satisfy_hunger", "--minutes", str(MAX_GOAL_MINUTES + 1))

    assert code == EXIT_USAGE
    assert str(MAX_GOAL_MINUTES) in world.stderr


# ---------------------------------------------------------------------------
# submit: the wire
# ---------------------------------------------------------------------------


def test_a_submitted_goal_reaches_a_real_core_and_the_place_is_reported(
    world: CliWorld, start_core: Start
) -> None:
    """The whole claim, end to end.

    The assertion is on what the *server* received: a command that refused
    locally would leave ``seen`` empty however plausible its output.

    The queue position is read from a second ``goal.status`` rather than from
    the admitted record's ``sequence``, which is the order it was admitted in
    and not its place in a backlog other goals have left.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": False, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status(pending=[a_goal_body()])

    code = world.run("goal", "submit", "satisfy_hunger", "--param", "satisfy_to=0.8")

    assert code == EXIT_OK
    assert core.methods == ["goal.submit", "goal.status"]
    submitted = core.params_of("goal.submit")
    assert submitted["kind"] == "satisfy_hunger"
    assert submitted["params"] == {"satisfy_to": 0.8}
    assert "goal-1" in world.stdout
    assert "1 of 1 waiting" in world.stdout


def test_the_idempotency_key_is_fresh_for_every_invocation(
    world: CliWorld, start_core: Start
) -> None:
    """A command run twice has asked twice.

    A key derived from the arguments would make the second run a silent no-op
    reporting the first run's goal, which is the opposite of what a user who
    typed the command again meant.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": False, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status()

    world.run("goal", "submit", "satisfy_hunger")
    world.run("goal", "submit", "satisfy_hunger")

    keys = [request.params["idempotency_key"] for request in core.seen if "kind" in request.params]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_the_care_parameters_cross_the_link_the_command_uses(
    world: CliWorld, start_core: Start
) -> None:
    """``rest_until`` is submittable, which it was not before the codec carried it.

    The value on the wire is the assertion. A codec that dropped it would leave
    the command sending a rest goal with no target, and the channel refusing a
    request the user filled in correctly.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {
        "duplicate": False,
        "goal": a_goal_body(kind="rest_until", params={"target_endurance": 0.8}),
    }
    core.answers["goal.status"] = a_status()

    code = world.run("goal", "submit", "rest_until", "--param", "target_endurance=0.8")

    assert code == EXIT_OK
    assert core.params_of("goal.submit")["params"] == {"target_endurance": 0.8}


def test_a_sleep_goal_carries_the_hours_it_was_given(world: CliWorld, start_core: Start) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {
        "duplicate": False,
        "goal": a_goal_body(kind="sleep_until_rested", params={"hours": 6}),
    }
    core.answers["goal.status"] = a_status()

    assert world.run("goal", "submit", "sleep_until_rested", "--param", "hours=6") == EXIT_OK
    assert core.params_of("goal.submit")["params"] == {"hours": 6}


def test_minutes_becomes_a_whole_budget_and_leaves_the_kind_s_other_two_bounds(
    world: CliWorld, start_core: Start
) -> None:
    """Wall clock is the user's; steps and the pending TTL stay the kind's.

    A command line has no phrasing for either, and inventing one would be a
    number nobody chose — the reasoning ``VoiceSession._budget`` states for the
    same three fields.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": False, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status()
    declared = GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget

    world.run("goal", "submit", "satisfy_hunger", "--minutes", "5")

    assert core.params_of("goal.submit")["budget"] == {
        "max_wall_ms": 300_000,
        "max_steps": declared.max_steps,
        "pending_ttl_ms": declared.pending_ttl_ms,
    }


def test_no_minutes_sends_no_budget_at_all(world: CliWorld, start_core: Start) -> None:
    """Absent means "the kind's declared default", which is a statement.

    Materialising the default here would put a copy of ``GOAL_SPECS`` on the
    wire, and the two would disagree the moment the table moved.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": False, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status()

    world.run("goal", "submit", "satisfy_hunger")

    assert "budget" not in core.params_of("goal.submit")


def test_a_goal_the_channel_refuses_is_reported_as_not_landed(
    world: CliWorld, start_core: Start
) -> None:
    """A refusal arrives inside the admission, and is not an unreachable link.

    The exit code separates them for a script, and the message separates them
    for a person: the channel answered, and what it answered was no.
    """
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {
        "duplicate": False,
        "refusal": {
            "reason_code": "NOT_ARMED",
            "message": "the session is not armed; run 'pz-agent arm'",
            "active_goal_id": "",
        },
    }

    code = world.run("goal", "submit", "satisfy_hunger")

    assert code == EXIT_FAILURE
    assert "not armed" in world.stderr
    # The channel was asked; nothing was assumed about the session locally.
    assert core.methods == ["goal.submit"]


def test_a_duplicate_submission_says_so_rather_than_counting_twice(
    world: CliWorld, start_core: Start
) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": True, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status(pending=[a_goal_body()])

    code = world.run("goal", "submit", "satisfy_hunger")

    assert code == EXIT_OK
    assert "already" in world.stdout


def test_a_submit_with_no_sidecar_names_the_command_that_starts_one(
    world: CliWorld,
) -> None:
    """The state every machine is in until the sidecar is started.

    "Unreachable" on its own sends a user looking for a broken install, so the
    remedy names the command. Both sentences are printed: the link's own, which
    says what was missing, and this command's, which says what to do.
    """
    code = world.run("goal", "submit", "satisfy_hunger")

    assert code == EXIT_FAILURE
    assert NO_SIDECAR_REMEDY in world.stderr
    assert "pz-agent play" in world.stderr


def test_a_core_that_refuses_the_method_is_not_reported_as_a_landed_goal(
    world: CliWorld, start_core: Start
) -> None:
    """A sidecar from another build accepts the connection and has no method.

    Every reading that stops at "a socket accepted me" reports the goal
    submitted. The answer has to be the answer, and this one is a refusal.
    """
    core = start_core(world.state_dir)

    code = world.run("goal", "submit", "satisfy_hunger")

    assert code == EXIT_FAILURE
    assert core.methods == ["goal.submit"], "the command decided without asking"
    assert "pz-agent play" in world.stderr


def test_submit_json_carries_the_goal_and_its_place(world: CliWorld, start_core: Start) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.submit"] = {"duplicate": False, "goal": a_goal_body()}
    core.answers["goal.status"] = a_status(pending=[a_goal_body()])

    assert world.run("goal", "submit", "satisfy_hunger", "--json") == EXIT_OK

    printed = document(world)
    assert printed["submitted"] is True
    assert printed["goal"]["goal_id"] == "goal-1"
    assert printed["place"]["position"] == 1
    # The fingerprint of the caller's key is not republished; the id is enough.
    assert "key_digest" not in printed["goal"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_the_active_goal_the_backlog_and_the_tails(
    world: CliWorld, start_core: Start
) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        active=a_goal_body(
            goal_id="active-goal", kind="loot_area", state="active", started_at_ms=1_200
        ),
        pending=[a_goal_body(goal_id="waiting-goal")],
        progress={"phase": "approach", "counters": {"containers": 2}},
        paused={
            "goal_id": "parked-goal",
            "kind": "navigate_to",
            "reason": "manual takeover",
            "paused_at_ms": 4_000,
        },
    )

    code = world.run("goal", "status")

    assert code == EXIT_OK
    assert core.methods == ["goal.status"]
    assert "active-goal" in world.stdout
    assert "waiting-goal" in world.stdout
    assert "approach" in world.stdout
    assert "containers=2" in world.stdout
    assert "manual takeover" in world.stdout


def test_an_active_goal_body_active_needs_its_start(world: CliWorld, start_core: Start) -> None:
    """A core answering an active goal with no start time is unreadable, loudly.

    The record's own invariant, arriving through the command: an active goal
    must record when it started, and a channel that says otherwise is refused
    rather than rendered.
    """
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        active=a_goal_body(goal_id="active-goal", state="active")
    )

    code = world.run("goal", "status")

    assert code == EXIT_FAILURE
    assert core.methods == ["goal.status"]


def test_a_suspended_goal_in_the_backlog_says_what_it_stepped_aside_for(
    world: CliWorld, start_core: Start
) -> None:
    """ "Waiting its turn" and "parked mid-mission" are different answers.

    They are the same line without the suspension fields, which is what this
    command printed while the codec dropped them.
    """
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        pending=[
            a_goal_body(
                goal_id="parked-goal",
                suspended_by="preempting-goal",
                suspensions=1,
                active_ms_before_suspend=7_000,
                front_rank=-1,
            )
        ]
    )

    assert world.run("goal", "status") == EXIT_OK
    assert "preempting-goal" in world.stdout


def test_an_answer_without_the_tails_prints_unreported_and_never_no(
    world: CliWorld, start_core: Start
) -> None:
    """The load-bearing rendering rule, against a peer that carries no tails.

    ``progress`` and ``paused`` decode to ``None`` for two different reasons —
    there is no such thing, or nothing was said — and the command cannot tell
    them apart. So it says neither: printing "paused: no" here would be a claim
    about a takeover this process was never told about.
    """
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        active=a_goal_body(goal_id="active-goal", state="active", started_at_ms=1_200)
    )

    assert world.run("goal", "status") == EXIT_OK

    printed = world.stdout
    assert printed.count(UNREPORTED) >= 2
    for forbidden in ("paused    no", "paused: no", "paused                 no"):
        assert forbidden not in printed
    assert "false" not in printed.casefold()


def test_status_with_an_id_reports_that_goal_in_detail(world: CliWorld, start_core: Start) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        named=a_goal_body(
            goal_id="named-goal",
            kind="train_skill",
            params={"skill": "carpentry", "target_level": 4},
        )
    )

    code = world.run("goal", "status", "named-goal")

    assert code == EXIT_OK
    assert core.params_of("goal.status")["goal_id"] == "named-goal"
    assert "named-goal" in world.stdout
    assert "skill=carpentry" in world.stdout
    assert "target_level=4" in world.stdout


def test_an_id_the_channel_does_not_know_is_not_reported_as_no_goal_running(
    world: CliWorld, start_core: Start
) -> None:
    """Two different answers, and only one of them means the goal is gone."""
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(active=a_goal_body(state="pending"))

    code = world.run("goal", "status", "no-such-goal")

    assert code == EXIT_FAILURE
    assert "no-such-goal" in world.stderr


def test_status_json_leaves_an_unreported_tail_as_null(world: CliWorld, start_core: Start) -> None:
    """A consumer branching on it must not have to match a word."""
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(pending=[a_goal_body()])

    assert world.run("goal", "status", "--json") == EXIT_OK

    printed = document(world)
    assert printed["progress"] is None
    assert printed["paused"] is None
    assert printed["report"] is None
    assert [record["goal_id"] for record in printed["pending"]] == ["goal-1"]


def test_a_status_with_no_sidecar_names_the_command_that_starts_one(
    world: CliWorld,
) -> None:
    code = world.run("goal", "status")

    assert code == EXIT_FAILURE
    assert NO_SIDECAR_REMEDY in world.stderr


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def cancelling(state: str = "active") -> Callable[[RpcRequest], JsonDict]:
    """A core that answers a cancel about whichever id it was asked about."""

    def answer(request: RpcRequest) -> JsonDict:
        goal_id = str(request.params["goal_id"])
        return {
            "requested": True,
            "goal": a_goal_body(goal_id=goal_id, state=state, started_at_ms=1_200),
        }

    return answer


def test_one_goal_is_cancelled_by_id(world: CliWorld, start_core: Start) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.cancel"] = cancelling()

    code = world.run("goal", "cancel", "goal-1")

    assert code == EXIT_OK
    assert core.methods == ["goal.cancel"]
    assert core.params_of("goal.cancel")["goal_id"] == "goal-1"
    assert "goal-1" in world.stdout


def test_a_cancellation_is_reported_as_requested_and_not_as_done(
    world: CliWorld, start_core: Start
) -> None:
    """The channel applies it on its next tick, so the goal is still running.

    Saying "cancelled" here would be the early claim the whole build refuses to
    make about a postcondition it has not observed.
    """
    core = start_core(world.state_dir)
    core.answers["goal.cancel"] = cancelling()

    world.run("goal", "cancel", "goal-1")

    assert "cancellation requested" in world.stdout
    # And the goal's own state is reported as what the channel said it is,
    # which is still running: the word for what happened is "requested".
    assert "the goal is active" in world.stdout


def test_a_goal_that_had_already_ended_is_an_answer_not_a_failure(
    world: CliWorld, start_core: Start
) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.cancel"] = {
        "requested": False,
        "goal": a_goal_body(
            state="succeeded",
            started_at_ms=1_200,
            finished_at_ms=2_000,
            reason_code="POSTCONDITION_MET",
            evidence_keys=["nutrition"],
        ),
    }

    code = world.run("goal", "cancel", "goal-1")

    assert code == EXIT_OK
    assert "already ended" in world.stdout


def test_an_id_the_channel_never_minted_is_reported_as_unknown(
    world: CliWorld, start_core: Start
) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.cancel"] = {"requested": False, "goal": None}

    assert world.run("goal", "cancel", "no-such-goal") == EXIT_OK
    assert "not holding a goal with this id" in world.stdout


def test_cancel_all_iterates_exactly_the_ids_one_status_answer_named(
    world: CliWorld, start_core: Start
) -> None:
    """One snapshot, and the loop is bounded by it.

    The active goal first, then the backlog oldest first. Re-reading the
    channel to chase goals that arrived while it was cancelling would be an
    unbounded loop, and this command does not run one.
    """
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(
        active=a_goal_body(goal_id="active-goal", state="active", started_at_ms=1_200),
        pending=[a_goal_body(goal_id="waiting-1"), a_goal_body(goal_id="waiting-2")],
    )
    core.answers["goal.cancel"] = cancelling()

    code = world.run("goal", "cancel", "--all")

    assert code == EXIT_OK
    assert core.methods == ["goal.status", "goal.cancel", "goal.cancel", "goal.cancel"]
    assert [
        request.params["goal_id"] for request in core.seen if request.method == "goal.cancel"
    ] == ["active-goal", "waiting-1", "waiting-2"]


def test_cancel_all_on_an_empty_channel_cancels_nothing_and_says_so(
    world: CliWorld, start_core: Start
) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status()

    code = world.run("goal", "cancel", "--all")

    assert code == EXIT_OK
    assert core.methods == ["goal.status"]
    assert "nothing to cancel" in world.stdout


def test_cancel_needs_exactly_one_of_an_id_and_all(world: CliWorld) -> None:
    assert world.run("goal", "cancel") == EXIT_USAGE
    assert world.run("goal", "cancel", "goal-1", "--all") == EXIT_USAGE


def test_a_cancel_with_no_sidecar_names_the_command_that_starts_one(
    world: CliWorld,
) -> None:
    code = world.run("goal", "cancel", "--all")

    assert code == EXIT_FAILURE
    assert NO_SIDECAR_REMEDY in world.stderr


def test_cancel_json_reports_every_goal_it_asked_about(world: CliWorld, start_core: Start) -> None:
    core = start_core(world.state_dir)
    core.answers["goal.status"] = a_status(pending=[a_goal_body(goal_id="waiting-1")])
    core.answers["goal.cancel"] = cancelling(state="pending")

    assert world.run("goal", "cancel", "--all", "--json") == EXIT_OK

    printed = document(world)
    assert [outcome["goal_id"] for outcome in printed["cancelled"]] == ["waiting-1"]
    assert printed["cancelled"][0]["requested"] is True


# ---------------------------------------------------------------------------
# the request builder, below the streams
# ---------------------------------------------------------------------------


class TestTheRequestBuilder:
    """What ``build_request`` makes of an argument vector, without a link."""

    def test_a_typed_value_is_read_as_its_parameter_s_type(self) -> None:
        request = build_request(
            "train_skill", params=["skill=first aid", "target_level=4"], minutes=None
        )

        assert request.kind is GoalKind.TRAIN_SKILL
        assert request.params == GoalParams(skill=TrainableSkill.FIRST_AID, target_level=4)
        # A number, not the string it was typed as: a wire carrying "4" here
        # would be refused by the channel's own range check.
        assert isinstance(request.params.target_level, int)

    def test_a_flag_is_a_boolean_and_false_is_a_choice(self) -> None:
        """``take_all=false`` is the caller choosing the useful-only default.

        Distinct from leaving it out, which is why the parameter is carried at
        all rather than folded into truthiness.
        """
        chosen = build_request("loot_area", params=["take_all=false"], minutes=None)

        assert chosen.params.take_all is False
        assert chosen.params.present() == frozenset({"take_all"})
        assert build_request("loot_area", params=[], minutes=None).params.take_all is None

    def test_every_kind_this_build_carries_can_be_built(self) -> None:
        """Driven off the enum, with the parameters each kind requires.

        The two care kinds are here because they could not be built before: the
        codec did not carry their parameters, so a submission of ``rest_until``
        could only ever be refused on the other side of the link.
        """
        required: dict[GoalKind, list[str]] = {
            GoalKind.TRAIN_SKILL: ["skill=carpentry"],
            GoalKind.NAVIGATE_TO: ["target_x=1200", "target_y=3400", "target_z=0"],
            GoalKind.REST_UNTIL: ["target_endurance=0.8"],
            # The crafting kind is the first that cannot be submitted bare: a
            # craft order with no product names nothing to make, so its
            # required parameter is the one thing a caller must always type.
            GoalKind.CRAFT_ITEM: ["product=Base.SpearCrude"],
        }
        for kind in GoalKind:
            request = build_request(kind.value, params=required.get(kind, []), minutes=None)
            assert request.kind is kind
