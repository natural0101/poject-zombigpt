"""The public reads over submitted work, and the session status that cannot lie.

The live session on Build 42.20.2 produced two facts this file pins down. A
mutating action sat in ``accepted`` forever and no public MCP tool could ask
about it — the only status read was on the Core RPC surface, and the refusal
text pointed clients at a method they could not call. And ``pz_session_status``
reported the sidecar's own armed flag while the game itself was running OFF, so
the one call meant to describe the session described half of it.

So the properties under test are about honesty at the edge, not plumbing:

* ``pz_action_status`` answers three different situations as three different
  shapes — a terminal record with its evidence, a live record with no invented
  end, and an id nobody here holds as ``known: false`` with the likely causes,
  which is a typed answer a loop can branch on and never an error.
* ``pz_action_await`` is bounded twice (deadline and poll count, on an injected
  clock, so these tests land on the deadline instead of sleeping through it),
  reports a budget that ran out as the *call's* end beside the record as it
  stands, and answers an unknown id immediately.
* ``pz_action_cancel_all`` submits exactly the mass ``plan.cancel`` — no
  ``command_id``, nothing narrower to mis-aim — is safe to repeat, and answers
  ``null`` for the per-layer counts no port carries rather than a number nobody
  measured.
* ``pz_session_status`` puts the game's own last word beside the sidecar's
  flags, keeps "the game has said nothing" distinct from agreement, and says
  which word to trust when they disagree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from pz_agent_core.protocol import ActionName, ReasonCode, SessionMode
from pz_agent_mcp.ports import ActionRecord
from pz_agent_mcp.router import ACTION_WAIT_POLL_MS, UNKNOWN_ACTION_CAUSES, ToolRouter
from tests.fixtures import DEFAULT_SESSION, make_observation, make_safety
from tests.fixtures.mcp_doubles import (
    CountingIds,
    Doubles,
    FakeActionPort,
    succeeded_result,
)

#: An id shaped like every real action id and minted by nobody: the unknown-here
#: paths must be reachable without depending on what the fake port happens to
#: have forgotten.
UNKNOWN_ID = str(uuid.UUID(int=0xDEAD))

POLL_S = ACTION_WAIT_POLL_MS / 1000.0


class FakeTime:
    """A clock and a sleep that agree, so a wait test lands on its deadline.

    Same construction as the CLI's control-waiter tests: every sleep advances
    the monotonic reading by exactly what was slept, which is what lets an
    assertion say "the budget ended after N polls" instead of "roughly".
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class SettlingActionPort(FakeActionPort):
    """A port whose record turns terminal on the Nth status read.

    Local rather than a fixture: the shared doubles deliberately have no
    "settle later" knob, and the wait tool is the one caller that needs a record
    to change *between* reads of the same port.
    """

    settle_on_read: int = 3
    reads: int = field(default=0)

    def status(self, action_id: str) -> ActionRecord | None:
        self.reads += 1
        if self.reads >= self.settle_on_read:
            record = self.records.get(action_id)
            if record is not None and not record.terminal:
                self.finish(action_id, succeeded_result(record.action))
        return super().status(action_id)


def make_router(doubles: Doubles, clock: FakeTime | None = None) -> ToolRouter:
    ticker = clock if clock is not None else FakeTime()
    return ToolRouter(
        doubles.services,
        request_ids=CountingIds(),
        sleep=ticker.sleep,
        monotonic=ticker.monotonic,
    )


def submit_one_action(router: ToolRouter) -> str:
    """Put one real submission through the router and return its id."""
    payload = router.call("pz_action_wait", {"game_seconds": 5, "idempotency_key": "seed-1"})
    assert payload["ok"] is True, payload
    action_id = payload["action_id"]
    assert isinstance(action_id, str)
    return action_id


# -- pz_action_status: three situations, three honest shapes ----------------


def test_a_terminal_record_answers_with_its_status_and_its_evidence() -> None:
    doubles = Doubles()
    router = make_router(doubles)
    action_id = submit_one_action(router)
    doubles.actions.finish(action_id, succeeded_result(ActionName.ACTION_WAIT))

    payload = router.call("pz_action_status", {"action_id": action_id})

    assert payload["ok"] is True
    assert payload["status"] == "succeeded"
    assert payload["action_id"] == action_id
    data = payload["data"]
    assert data["known"] is True
    assert data["terminal"] is True
    assert data["reason_code"] == ReasonCode.POSTCONDITION_MET.value
    assert data["evidence"]["observed"], "an observed success must carry its postcondition"


def test_a_live_record_is_reported_live_with_no_invented_end() -> None:
    doubles = Doubles()
    router = make_router(doubles)
    action_id = submit_one_action(router)

    payload = router.call("pz_action_status", {"action_id": action_id})

    assert payload["ok"] is True
    assert payload["status"] == "accepted"
    data = payload["data"]
    assert data["known"] is True
    assert data["terminal"] is False
    assert "evidence" not in data
    assert "reason_code" not in data, "a live action has no terminal reason to report"


def test_an_unknown_id_is_a_typed_answer_naming_the_likely_causes() -> None:
    router = make_router(Doubles())

    payload = router.call("pz_action_status", {"action_id": UNKNOWN_ID})

    assert payload["ok"] is True, "unknown-here is an answer, not an error"
    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["known"] is False
    assert data["status"] is None
    assert data["terminal"] is None
    assert data["action_id"] == UNKNOWN_ID
    assert data["likely_causes"] == list(UNKNOWN_ACTION_CAUSES)
    assert "not 'it did not run'" in payload["message"]


# -- pz_action_await: bounded, lock-free, honest about whose end it was -----


def test_await_returns_as_soon_as_the_record_turns_terminal() -> None:
    doubles = Doubles()
    doubles.actions = SettlingActionPort(settle_on_read=3)
    clock = FakeTime()
    router = make_router(doubles, clock)
    action_id = submit_one_action(router)

    payload = router.call("pz_action_await", {"action_id": action_id, "timeout_ms": 60_000})

    assert payload["ok"] is True
    assert payload["status"] == "succeeded"
    data = payload["data"]
    assert data["known"] is True
    assert data["terminal"] is True
    assert data["timed_out"] is False
    # Two polls between the three reads, at the published interval and no more:
    # the budget above would have allowed twelve hundred.
    assert data["waited_ms"] == 2 * ACTION_WAIT_POLL_MS
    assert clock.sleeps == [POLL_S, POLL_S]


def test_await_that_runs_out_of_budget_still_reports_the_live_record() -> None:
    doubles = Doubles()
    clock = FakeTime()
    router = make_router(doubles, clock)
    action_id = submit_one_action(router)

    payload = router.call("pz_action_await", {"action_id": action_id, "timeout_ms": 200})

    assert payload["ok"] is True
    assert payload["status"] == "accepted"
    data = payload["data"]
    assert data["known"] is True
    assert data["timed_out"] is True
    assert data["status"] == "accepted"
    assert data["waited_ms"] == 200
    assert "the wait budget ended, not the action" in payload["message"]
    # The deadline is the bound: 200 ms of budget is exactly four polls.
    assert clock.sleeps == [POLL_S] * 4


def test_await_answers_an_unknown_id_immediately() -> None:
    clock = FakeTime()
    router = make_router(Doubles(), clock)

    payload = router.call("pz_action_await", {"action_id": UNKNOWN_ID, "timeout_ms": 60_000})

    assert payload["ok"] is True
    data = payload["data"]
    assert data["known"] is False
    assert data["likely_causes"] == list(UNKNOWN_ACTION_CAUSES)
    assert data["waited_ms"] == 0
    assert data["timed_out"] is False, "waiting longer cannot make an unknown id appear"
    assert clock.sleeps == []


# -- pz_action_cancel_all: mass form, mod-owned only, honest about counts ---


def test_cancel_all_submits_exactly_the_mass_cancel() -> None:
    doubles = Doubles()
    router = make_router(doubles)

    payload = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})

    assert payload["ok"] is True
    request = doubles.actions.submitted[-1]
    assert request.action is ActionName.PLAN_CANCEL
    # No command_id is the spelling every cancel adapter reads as "clear
    # everything of ours"; anything else in args would aim the mass form.
    assert request.args == {}
    assert request.session_id == DEFAULT_SESSION
    data = payload["data"]
    assert data["scope"] == "mod_owned"
    assert data["requested_reason"] == ReasonCode.CANCELLED_BY_REQUEST.value
    assert payload["action_id"], "the cancel is awaitable like any other submission"


def test_cancel_all_reports_uncounted_layers_as_null_never_as_zero() -> None:
    router = make_router(Doubles())

    payload = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})

    assert payload["data"]["cancelled_counts"] == {"channel_pending": None, "in_flight": None}
    assert any("uncounted, never zero" in warning for warning in payload["warnings"])


def test_cancel_all_twice_is_two_honest_calls_and_a_replayed_key_is_no_third() -> None:
    doubles = Doubles()
    router = make_router(doubles)

    first = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})
    second = router.call("pz_action_cancel_all", {"idempotency_key": "ca-2"})

    # The postcondition is negative, so cancelling what is already gone is a
    # success, not a conflict: both calls submit and both are answered.
    assert first["ok"] is True and second["ok"] is True
    assert len(doubles.actions.submitted) == 2

    replayed = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})

    assert replayed["ok"] is True
    assert replayed["replayed"] is True
    assert len(doubles.actions.submitted) == 2, "a replayed key must not cancel again"


def test_cancel_all_works_on_a_disarmed_session() -> None:
    doubles = Doubles()
    doubles.session.snapshot = replace(
        doubles.session.snapshot, armed=False, mode=SessionMode.OBSERVE
    )
    router = make_router(doubles)

    payload = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})

    assert payload["ok"] is True, "a cancel gated on arming would be unusable when needed"


def test_cancel_all_settles_through_await_with_the_engines_own_evidence() -> None:
    doubles = Doubles()
    router = make_router(doubles)
    payload = router.call("pz_action_cancel_all", {"idempotency_key": "ca-1"})
    action_id = payload["action_id"]
    doubles.actions.finish(
        action_id,
        succeeded_result(
            ActionName.PLAN_CANCEL,
            observed={"busy": False, "cancelled_command_id": None},
            kind="no_session_owned_action_in_flight",
        ),
    )

    settled = router.call("pz_action_await", {"action_id": action_id, "timeout_ms": 1_000})

    assert settled["status"] == "succeeded"
    assert settled["data"]["evidence"]["kind"] == "no_session_owned_action_in_flight"


# -- pz_session_status: the game's word beside the sidecar's ----------------


def test_sidecar_armed_while_the_game_says_off_is_visible_in_one_call() -> None:
    doubles = Doubles()
    doubles.observations.observation = make_observation(
        safety=make_safety(armed=False, mode=SessionMode.OBSERVE)
    )
    router = make_router(doubles)

    payload = router.call("pz_session_status", {})

    data = payload["data"]
    # The sidecar's own flags, unrenamed for existing clients.
    assert data["armed"] is True
    assert data["mode"] == SessionMode.ASSISTED.value
    assert data["desired_mode"] == SessionMode.ASSISTED.value
    # The game's last word, beside them rather than averaged with them.
    assert data["game_armed"] is False
    assert data["effective_mode"] == SessionMode.OBSERVE.value
    assert data["game_session_id"] == DEFAULT_SESSION
    assert data["game_view_seq"] == 1
    assert data["armed_mismatch"] is True
    assert any("arming disagreement" in warning for warning in payload["warnings"])
    assert "armed=True" in payload["message"] and "armed=False" in payload["message"]


def test_a_game_that_has_said_nothing_is_unknown_not_agreement() -> None:
    doubles = Doubles()
    doubles.observations.observation = None
    router = make_router(doubles)

    payload = router.call("pz_session_status", {})

    data = payload["data"]
    assert data["game_armed"] is None
    assert data["effective_mode"] is None
    assert data["game_session_id"] is None
    assert data["game_view_seq"] is None
    assert data["armed_mismatch"] is None, "absent-as-False would manufacture agreement"
    assert payload["warnings"] == []


def test_agreement_is_reported_quietly() -> None:
    router = make_router(Doubles())

    payload = router.call("pz_session_status", {})

    data = payload["data"]
    assert data["armed_mismatch"] is False
    assert data["game_armed"] is True
    assert data["effective_mode"] == data["mode"]
    assert payload["warnings"] == []
    assert payload["message"] == "session status"


def test_a_stale_heartbeat_beside_a_disagreement_is_called_out() -> None:
    doubles = Doubles()
    doubles.session.snapshot = replace(doubles.session.snapshot, game_heartbeat_ok=False)
    doubles.observations.observation = make_observation(
        safety=make_safety(armed=False, mode=SessionMode.OBSERVE)
    )
    router = make_router(doubles)

    payload = router.call("pz_session_status", {})

    assert payload["data"]["armed_mismatch"] is True
    assert any("stale" in warning for warning in payload["warnings"])
