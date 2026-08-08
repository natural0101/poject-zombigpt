"""The remote action channel: bounded admission, honest records, the levers.

Two layers, deliberately. The :class:`~pz_agent_cli.runtime.ActionChannel`
tests exercise the queue and record store directly — the cap, the idempotency
resolution, the eviction rule that never forgets in-flight work, the
cross-thread visibility of whole-replaced frozen records. The loop tests drive
a real :class:`~pz_agent_cli.runtime.SidecarLoop` over a real exchange
directory (the mod faked at the files, as everywhere in this suite) and prove
what the channel's records claim: a submission while disarmed ends with the
channel's ``NOT_ARMED`` refusal on the next tick, a panic sentinel ends it as
``PANIC_STOP``, a shutdown as ``CANCELLED_BY_REQUEST``, and an armed dispatch
runs through the *same* engine a planner proposal does — all the way to the
engine's own ``SUCCEEDED`` with the observed world-time evidence only that
engine can mint. The cross-process proof over the RPC socket lives in
``tests/contract/test_remote_actions_served.py``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pz_agent_cli.core_services import (
    REMOTE_ACTIONS_UNSERVED,
    LoopActionPort,
    core_services_over,
)
from pz_agent_cli.doctor import DoctorReport
from pz_agent_cli.runtime import ActionChannel, LoopError
from pz_agent_core.actions import ActionRequest
from pz_agent_core.protocol import ActionName, ActionResult, ActionStatus, ReasonCode
from pz_agent_mcp.ports import ActionRecord
from tests.fixtures import make_game
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.sidecar_worlds import SidecarWorld, attached_world, make_sidecar_world

#: A session id shaped like the ones the loop mints; the channel never checks
#: it — the engine does — so any stable value serves the queue-level tests.
SESSION = "11111111-1111-1111-1111-111111111111"


def _clock() -> int:
    return BASE_TIME_MS


def _request(key: str, *, seconds: float = 1.0) -> ActionRequest:
    return ActionRequest(
        action=ActionName.ACTION_WAIT,
        session_id=SESSION,
        idempotency_key=key,
        args={"game_seconds": seconds},
    )


def _terminal(request: ActionRequest, command_id: str) -> ActionResult:
    return ActionResult.failure(
        session_id=request.session_id,
        seq=1,
        command_id=command_id,
        action=request.action.value,
        timestamp_ms=BASE_TIME_MS,
        reason_code=ReasonCode.ACTION_TIMEOUT,
        status=ActionStatus.FAILED,
        message="the postcondition was never observed",
    )


def _empty_doctor() -> DoctorReport:
    return DoctorReport(checks=(), generated_at="2026-08-08T00:00:00+00:00")


class TestAdmission:
    def test_admission_returns_the_queued_record_and_status_answers_it(self) -> None:
        channel = ActionChannel(clock=_clock)

        record = channel.submit(_request("k-1"))

        assert record.status is ActionStatus.ACCEPTED, "queued is 'accepted', never more"
        assert record.action is ActionName.ACTION_WAIT
        assert record.idempotency_key == "k-1"
        assert channel.status(record.action_id) == record
        assert channel.pending_count == 1

    def test_a_full_queue_is_a_named_refusal_never_a_silent_drop(self) -> None:
        channel = ActionChannel(clock=_clock, max_pending=2, max_remembered=8)
        first = channel.submit(_request("k-1"))
        second = channel.submit(_request("k-2"))

        with pytest.raises(LoopError, match=r"already holds 2 submission\(s\)"):
            channel.submit(_request("k-3"))

        # The refusal cost nothing that was already admitted.
        assert channel.status(first.action_id) is not None
        assert channel.status(second.action_id) is not None
        assert channel.pending_count == 2

    def test_a_resubmitted_key_resolves_to_the_record_it_created(self) -> None:
        channel = ActionChannel(clock=_clock)
        first = channel.submit(_request("k-1"))

        again = channel.submit(_request("k-1"))

        assert again.action_id == first.action_id, "a retried submission is never a second action"
        assert channel.pending_count == 1

    def test_a_key_reused_for_different_work_is_refused_as_a_caller_bug(self) -> None:
        channel = ActionChannel(clock=_clock)
        channel.submit(_request("k-1", seconds=1.0))

        with pytest.raises(LoopError, match="already used for a different action"):
            channel.submit(_request("k-1", seconds=2.0))

    def test_status_of_an_id_never_minted_is_none(self) -> None:
        assert ActionChannel(clock=_clock).status("never-minted") is None


class TestTheTickThreadHalf:
    def test_take_next_marks_started_and_settle_records_the_terminal_result(self) -> None:
        channel = ActionChannel(clock=_clock)
        record = channel.submit(_request("k-1"))

        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        assert action_id == record.action_id
        started = channel.status(action_id)
        assert started is not None and started.status is ActionStatus.STARTED

        settled = channel.settle(action_id, _terminal(request, "cmd-1"))

        assert settled.status is ActionStatus.FAILED
        assert settled.result is not None
        assert settled.result.reason_code is ReasonCode.ACTION_TIMEOUT
        assert channel.status(action_id) == settled
        assert channel.pending_count == 0

    def test_take_next_answers_none_when_nothing_waits(self) -> None:
        assert ActionChannel(clock=_clock).take_next() is None

    def test_settle_refuses_a_non_terminal_result(self) -> None:
        channel = ActionChannel(clock=_clock)
        record = channel.submit(_request("k-1"))
        channel.take_next()
        not_terminal = ActionResult.failure(
            session_id=SESSION,
            seq=1,
            command_id="cmd-1",
            action=ActionName.ACTION_WAIT.value,
            timestamp_ms=BASE_TIME_MS,
            reason_code=ReasonCode.ACTION_TIMEOUT,
            status=ActionStatus.STARTED,
        )

        with pytest.raises(LoopError, match="only a terminal result settles"):
            channel.settle(record.action_id, not_terminal)

    def test_settle_refuses_an_id_never_minted(self) -> None:
        channel = ActionChannel(clock=_clock)
        with pytest.raises(LoopError, match="never minted"):
            channel.settle("never-minted", _terminal(_request("k"), "cmd-1"))


class TestClearing:
    def test_clear_pending_ends_every_waiting_submission_and_says_why(self) -> None:
        channel = ActionChannel(clock=_clock)
        first = channel.submit(_request("k-1"))
        second = channel.submit(_request("k-2"))

        ended = channel.clear_pending(
            ReasonCode.NOT_ARMED,
            status=ActionStatus.REJECTED,
            message="the sidecar is not armed",
        )

        assert [record.action_id for record in ended] == [first.action_id, second.action_id]
        assert channel.pending_count == 0
        for record in ended:
            assert record.status is ActionStatus.REJECTED
            assert record.result is not None
            assert record.result.reason_code is ReasonCode.NOT_ARMED
            assert "not armed" in record.result.message

    def test_clearing_leaves_a_taken_submission_to_the_engine(self) -> None:
        channel = ActionChannel(clock=_clock)
        record = channel.submit(_request("k-1"))
        channel.take_next()

        assert (
            channel.clear_pending(
                ReasonCode.PANIC_STOP, status=ActionStatus.CANCELLED, message="panic"
            )
            == ()
        )
        started = channel.status(record.action_id)
        assert started is not None and started.status is ActionStatus.STARTED

    def test_clearing_refuses_to_mark_anything_succeeded_or_non_terminal(self) -> None:
        channel = ActionChannel(clock=_clock)
        with pytest.raises(LoopError, match="must end submissions"):
            channel.clear_pending(
                ReasonCode.NOT_ARMED, status=ActionStatus.ACCEPTED, message="nope"
            )


class TestBounds:
    def test_the_constructor_enforces_its_own_bounds(self) -> None:
        with pytest.raises(LoopError, match="max_pending must be positive"):
            ActionChannel(clock=_clock, max_pending=0)
        with pytest.raises(LoopError, match="max_remembered must exceed max_pending"):
            ActionChannel(clock=_clock, max_pending=4, max_remembered=4)

    def test_eviction_drops_the_oldest_terminal_record_and_only_terminal_ones(self) -> None:
        channel = ActionChannel(clock=_clock, max_pending=4, max_remembered=5)
        waiting = [channel.submit(_request(f"w-{i}")) for i in range(4)]

        # Finish the two oldest, freeing pending slots and creating terminals.
        finished: list[str] = []
        for _ in range(2):
            taken = channel.take_next()
            assert taken is not None
            action_id, request = taken
            channel.settle(action_id, _terminal(request, f"cmd-{action_id[:8]}"))
            finished.append(action_id)

        # Two more submissions push the store past its cap of five...
        channel.submit(_request("w-4"))
        channel.submit(_request("w-5"))

        # ...and only the finished records were forgotten, oldest first: every
        # record still waiting kept the handle its submitter polls.
        assert channel.remembered_count == 5
        assert channel.status(finished[0]) is None, "the oldest terminal record was evicted"
        assert channel.status(finished[1]) is not None
        for record in waiting[2:]:
            still_there = channel.status(record.action_id)
            assert still_there is not None
            assert still_there.status is ActionStatus.ACCEPTED

    def test_eviction_never_removes_in_flight_records_even_past_the_cap(self) -> None:
        # A shape the loop never produces (two submissions taken without being
        # settled) exists so the guard is provable: with no terminal victim the
        # store holds every open record rather than forgetting one.
        channel = ActionChannel(clock=_clock, max_pending=4, max_remembered=5)
        for i in range(4):
            channel.submit(_request(f"w-{i}"))
        channel.take_next()
        channel.take_next()
        channel.submit(_request("w-4"))
        channel.submit(_request("w-5"))

        assert channel.remembered_count == 6, "no terminal victim existed, so nothing was evicted"

    def test_an_evicted_key_starts_over_rather_than_resolving_to_nothing(self) -> None:
        channel = ActionChannel(clock=_clock, max_pending=1, max_remembered=2)
        first = channel.submit(_request("k-1"))
        taken = channel.take_next()
        assert taken is not None
        channel.settle(first.action_id, _terminal(taken[1], "cmd-1"))
        # Two more finished actions push k-1's record out of the history.
        for key in ("k-2", "k-3"):
            record = channel.submit(_request(key))
            taken = channel.take_next()
            assert taken is not None
            channel.settle(record.action_id, _terminal(taken[1], f"cmd-{key}"))

        again = channel.submit(_request("k-1"))

        assert again.action_id != first.action_id
        assert again.status is ActionStatus.ACCEPTED


class TestCrossThreadVisibility:
    def test_a_submission_from_another_thread_becomes_visible_whole(self) -> None:
        """The serving thread submits; this thread reads records, never halves.

        Bounded the way every wait in this suite is: a deadline and an
        iteration cap, so a scheduler hiccup is a failed assertion rather than
        a hang.
        """
        channel = ActionChannel(clock=_clock)
        ids: list[str] = []

        def serve() -> None:
            for i in range(3):
                ids.append(channel.submit(_request(f"t-{i}")).action_id)

        thread = threading.Thread(target=serve, name="fake-serving-thread")
        thread.start()
        thread.join(timeout=10.0)
        assert not thread.is_alive()

        deadline = time.monotonic() + 10.0
        for _ in range(1_000):
            if channel.pending_count == 3 or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert channel.pending_count == 3
        for action_id in ids:
            record = channel.status(action_id)
            assert record is not None
            assert record.status is ActionStatus.ACCEPTED
            assert record.action_id == action_id


# ---------------------------------------------------------------------------
# the channel on the loop, driven by real ticks
# ---------------------------------------------------------------------------


def _submit(world: SidecarWorld, key: str, *, seconds: float = 1.0) -> str:
    channel = world.loop.actions
    assert channel is not None, "the default assembly always builds a channel"
    record = channel.submit(
        ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=world.session_id,
            idempotency_key=key,
            args={"game_seconds": seconds},
        )
    )
    return record.action_id


def _record(world: SidecarWorld, action_id: str) -> ActionRecord:
    channel = world.loop.actions
    assert channel is not None
    record = channel.status(action_id)
    assert record is not None
    return record


class TestTheLoopServesTheChannel:
    def test_a_submission_while_disarmed_ends_on_the_next_tick_as_not_armed(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            action_id = _submit(world, "disarmed-1")

            world.loop.tick()

            record = _record(world, action_id)
            assert record.status is ActionStatus.REJECTED
            result = record.result
            assert result is not None
            assert result.reason_code is ReasonCode.NOT_ARMED
            assert "not armed" in result.message

    def test_disarm_itself_ends_waiting_submissions_without_waiting_for_a_tick(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            world.beat_game()
            assert world.loop.arm().armed is True
            action_id = _submit(world, "armed-then-disarmed")

            world.loop.disarm(reason="user asked")

            record = _record(world, action_id)
            assert record.status is ActionStatus.REJECTED
            result = record.result
            assert result is not None and result.reason_code is ReasonCode.NOT_ARMED

    def test_a_panic_tick_ends_waiting_submissions_with_the_panic_reason(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            world.beat_game()
            assert world.loop.arm().armed is True
            action_id = _submit(world, "panicked-1")
            world.panic()

            world.loop.tick()

            record = _record(world, action_id)
            assert record.status is ActionStatus.CANCELLED
            result = record.result
            assert result is not None and result.reason_code is ReasonCode.PANIC_STOP
            assert world.loop.armed is False

    def test_shutdown_ends_waiting_submissions_as_cancelled_by_request(
        self, tmp_path: Path
    ) -> None:
        world = attached_world(tmp_path)
        action_id = _submit(world, "shutdown-1")

        world.loop.shutdown(reason="test finished")

        record = _record(world, action_id)
        assert record.status is ActionStatus.CANCELLED
        result = record.result
        assert result is not None
        assert result.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert "shut down" in result.message

    def test_an_armed_dispatch_drives_the_real_engine_to_its_own_observed_success(
        self, tmp_path: Path
    ) -> None:
        """The strongest claim the channel can make, proven at the loop level.

        The fake mod advances the world clock on every wait the engine makes
        (``while_waiting``), so ``action.wait`` reaches the one exit the engine
        calls success: the adapter observed the postcondition — game time
        advanced past what was asked — and the record carries that evidence.
        Nothing acked anything; the world itself was the proof.
        """
        with attached_world(tmp_path) as world:
            world.beat_game()
            assert world.loop.arm().armed is True

            base = datetime(1993, 7, 9, 14, 20, 0)
            advanced = {"game_seconds": 0}

            def advance_world() -> None:
                advanced["game_seconds"] += 120
                stamp = (base + timedelta(seconds=advanced["game_seconds"])).isoformat()
                world.observe(game=make_game(world_time=stamp))

            world.sleeper.while_waiting = advance_world
            advance_world()  # one observation before the tick, so the loop can act

            action_id = _submit(world, "armed-wait-1", seconds=1.0)
            outcome = world.loop.tick()

            record = _record(world, action_id)
            assert record.status is ActionStatus.SUCCEEDED
            result = record.result
            assert result is not None
            assert result.reason_code is ReasonCode.POSTCONDITION_MET
            observed = result.evidence["observed"]
            assert observed["elapsed_game_seconds"] >= 1.0
            # The dispatch went through the tick's own funnel: the result is
            # this tick's, and the budget was spent exactly once for it.
            assert result in outcome.results
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 0


class TestTheServicesBundle:
    def test_a_loop_with_a_channel_is_served_through_the_loop_action_port(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            services = core_services_over(world.loop, doctor=_empty_doctor, log_file=None)

            assert isinstance(services.actions, LoopActionPort)
            record = services.actions.submit(
                ActionRequest(
                    action=ActionName.ACTION_WAIT,
                    session_id=world.session_id,
                    idempotency_key="svc-1",
                    args={"game_seconds": 1.0},
                )
            )
            assert record.status is ActionStatus.ACCEPTED
            assert services.actions.status(record.action_id) == record
            assert services.actions.status("never-minted") is None

    def test_a_loop_without_a_channel_keeps_the_named_refusal(self, tmp_path: Path) -> None:
        """Requirement six: no channel means the honest refusal, not a sink."""
        with attached_world(tmp_path) as world:
            world.loop.actions = None
            services = core_services_over(world.loop, doctor=_empty_doctor, log_file=None)

            with pytest.raises(LoopError) as refused:
                services.actions.submit(
                    ActionRequest(
                        action=ActionName.ACTION_WAIT,
                        session_id=world.session_id,
                        idempotency_key="svc-2",
                        args={"game_seconds": 1.0},
                    )
                )
            assert REMOTE_ACTIONS_UNSERVED in str(refused.value)
            assert services.actions.status("anything") is None

    def test_an_unattached_loop_refuses_to_admit_what_nothing_would_drain(
        self, tmp_path: Path
    ) -> None:
        world = make_sidecar_world(tmp_path)
        services = core_services_over(world.loop, doctor=_empty_doctor, log_file=None)

        with pytest.raises(LoopError, match="not attached"):
            services.actions.submit(
                ActionRequest(
                    action=ActionName.ACTION_WAIT,
                    session_id=SESSION,
                    idempotency_key="svc-3",
                    args={"game_seconds": 1.0},
                )
            )
