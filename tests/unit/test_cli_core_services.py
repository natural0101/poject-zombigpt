"""The `CoreServices` adapter over the real sidecar loop, port by port.

These are the unit-level halves of R-008's closure: each port is exercised
against a real :class:`~pz_agent_cli.runtime.SidecarLoop` over a real exchange
directory (the mod faked at the files), with the loop's ticks driven from
*inside* the adapter's bounded wait — the injected sleeper ticks the loop once
per poll, so the arm/disarm round trip through the shipped control channel is
deterministic and single-threaded here. The cross-process, cross-thread proof
lives in ``tests/contract/test_sidecar_serves_the_core.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.core_services import (
    ARM_NEEDS_BACKUP,
    CONTROL_UNANSWERED,
    REMOTE_ACTIONS_UNSERVED,
    REMOTE_PLANS_UNSERVED,
    LoopDiagnosticsPort,
    LoopGoalPort,
    LoopMemoryPort,
    core_services_over,
)
from pz_agent_cli.doctor import CheckResult, CheckStatus, DoctorReport
from pz_agent_cli.memory import SidecarMemory
from pz_agent_cli.runtime import CapabilityLedger, LoopError
from pz_agent_core.actions import ActionRequest
from pz_agent_core.diagnostics import DiagnosticLog
from pz_agent_core.goals import GoalKind, GoalQueue, GoalRequest
from pz_agent_core.memory import MemoryStore, Square
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import ActionName, Observation, SessionMode
from pz_agent_mcp.ports import CoreServices, PlanRequest
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.mcp_doubles import make_report
from tests.fixtures.sidecar_worlds import SidecarWorld, attached_world, make_sidecar_world


class Monotonic:
    """A monotonic clock the test owns, advancing a fixed step per read."""

    def __init__(self, step: float = 0.0001) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _services(world: SidecarWorld, *, ticking: bool = True, step: float = 0.0001) -> CoreServices:
    """The adapter over *world*'s loop, its waits driving the loop's own ticks."""

    def tick_once(_seconds: float) -> None:
        world.loop.tick()

    def never(_seconds: float) -> None:
        return None

    return core_services_over(
        world.loop,
        doctor=_empty_doctor,
        log_file=None,
        sleep=tick_once if ticking else never,
        monotonic=Monotonic(step=step),
    )


def _empty_doctor() -> DoctorReport:
    return DoctorReport(checks=(), generated_at="2026-08-08T00:00:00+00:00")


class _PlainPlanner:
    """A planner with no goal seam: goals must then not be served at all."""

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None


class _GoalCapablePlanner(_PlainPlanner):
    """The narrowest thing `GoalPlanner` accepts, for the wiring assertion."""

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        return None


def _an_action() -> ActionRequest:
    return ActionRequest(
        action=ActionName.CONSUME_EAT,
        session_id="11111111-1111-1111-1111-111111111111",
        idempotency_key="k-1",
        args={"item_ref": "i-1"},
    )


class TestSessionReads:
    def test_status_is_the_loop_s_own_state(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            services = _services(world)

            snapshot = services.session.status()

            assert snapshot.session_id == world.session_id
            assert snapshot.mode is SessionMode.OBSERVE
            assert snapshot.armed is False
            assert snapshot.connected is True, "the game heartbeat is fresh"
            assert snapshot.observation_seq is None, "nothing has been observed yet"

    def test_status_carries_the_latest_observation_seq(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            world.observe()
            world.observe()
            world.loop.tick()
            services = _services(world)

            assert services.session.status().observation_seq == 2

    def test_status_refuses_before_attach_rather_than_inventing_a_session(
        self, tmp_path: Path
    ) -> None:
        world = make_sidecar_world(tmp_path)
        services = _services(world)

        with pytest.raises(LoopError, match="not attached"):
            services.session.status()


class TestArmingTravelsTheControlChannel:
    def test_arm_is_applied_by_the_loop_and_observed_before_it_is_reported(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            services = _services(world)

            snapshot = services.session.arm(SessionMode.ASSISTED, confirm_backup=True)

            assert snapshot.armed is True
            assert snapshot.mode is SessionMode.ASSISTED
            assert world.loop.armed is True, (
                "the loop itself armed; nothing was mutated cross-thread"
            )

    def test_arm_without_a_backup_confirmation_is_refused_before_anything_travels(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            services = _services(world)

            with pytest.raises(LoopError, match="confirm_backup"):
                services.session.arm(SessionMode.ASSISTED, confirm_backup=False)

            assert ARM_NEEDS_BACKUP.startswith("arming was requested without confirm_backup")
            assert world.loop.armed is False
            channel = world.loop.control
            assert channel is not None and channel.pending() is False, (
                "a refused arm must not leave a request for the loop to consume later"
            )

    def test_a_refused_arm_relays_the_loop_s_own_words(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            world.panic()
            services = _services(world)

            with pytest.raises(LoopError, match="panic-stop sentinel"):
                services.session.arm(SessionMode.ASSISTED, confirm_backup=True)

            assert world.loop.armed is False

    def test_an_unconsumed_request_is_a_named_refusal_not_a_hang(self, tmp_path: Path) -> None:
        """The loop never ticks, so nobody judges the request; the wait is bounded."""
        with attached_world(tmp_path) as world:
            services = _services(world, ticking=False, step=0.5)

            with pytest.raises(LoopError) as unanswered:
                services.session.arm(SessionMode.ASSISTED, confirm_backup=True)

            assert CONTROL_UNANSWERED in str(unanswered.value)

    def test_disarm_returns_the_loop_to_observe(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            world.beat_game()
            assert world.loop.arm(SessionMode.ASSISTED).armed is True
            services = _services(world)

            snapshot = services.session.disarm()

            assert snapshot.armed is False
            assert snapshot.mode is SessionMode.OBSERVE
            assert world.loop.armed is False

    def test_stop_latches_the_panic_sentinel_and_reports_what_it_observed(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            world.observe()
            world.loop.tick()
            world.beat_game()
            assert world.loop.arm(SessionMode.ASSISTED).armed is True
            services = _services(world)

            report = services.session.stop()

            assert world.layout.panic_stop.is_file(), "the stop is a level the mod also reads"
            assert world.layout.panic_stop.stat().st_size > 0
            assert report.disarmed is True, "the loop's tick observed the latch and disarmed"
            assert report.mode is SessionMode.OBSERVE
            assert report.cleared == 0, "the mod owns that count; nothing here invents it"


class TestReadOnlyPorts:
    def test_the_latest_observation_is_the_store_s_own(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            written = world.observe()
            world.loop.tick()
            services = _services(world)

            assert services.observations.latest() == written

    def test_no_observation_yet_is_none_not_an_error(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            assert _services(world).observations.latest() is None

    def test_the_capability_report_is_the_ledger_s_own(self, tmp_path: Path) -> None:
        report = make_report(usable=["movement_walk"])
        with attached_world(
            tmp_path,
            capabilities=CapabilityLedger(
                record_path=tmp_path / "cap.json", report=report, detail="built by this test"
            ),
        ) as world:
            assert _services(world).capabilities.report() is report

    def test_an_unresolved_ledger_refuses_with_its_own_detail(self, tmp_path: Path) -> None:
        with (
            attached_world(
                tmp_path,
                capabilities=CapabilityLedger(
                    record_path=tmp_path / "cap.json",
                    report=None,
                    detail="the scan could not run: no installation",
                ),
            ) as world,
            pytest.raises(LoopError, match="no installation"),
        ):
            _services(world).capabilities.report()

    def test_a_loop_without_a_ledger_refuses_by_name(self, tmp_path: Path) -> None:
        with (
            attached_world(tmp_path) as world,
            pytest.raises(LoopError, match="without a capability ledger"),
        ):
            _services(world).capabilities.report()


class TestUnservedSurfacesRefuseByName:
    def test_actions_over_a_loop_without_a_channel_refuse_by_name(self, tmp_path: Path) -> None:
        """The default assembly serves actions over the loop's own channel
        (``tests/unit/test_action_channel.py`` is that proof); a loop stripped
        of the channel keeps the honest refusal, because admitting submissions
        nothing would ever drain is the forbidden port.
        """
        with attached_world(tmp_path) as world:
            world.loop.actions = None
            services = _services(world)

            with pytest.raises(LoopError) as refused:
                services.actions.submit(_an_action())

            assert REMOTE_ACTIONS_UNSERVED in str(refused.value)
            assert services.actions.status("never-minted") is None

    def test_plan_execute_refuses_and_plan_current_answers_none(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            services = _services(world)
            request = PlanRequest(
                goal="stock the base",
                mode=SessionMode.ASSISTED,
                max_steps=3,
                max_real_seconds=60,
                idempotency_key="p-1",
            )

            with pytest.raises(LoopError) as refused:
                services.plans.execute(request)

            assert REMOTE_PLANS_UNSERVED in str(refused.value)
            assert services.plans.current() is None

    def test_a_loop_without_a_queue_carries_no_goal_channel(self, tmp_path: Path) -> None:
        """`None` is the honest value: the router's NO_GOAL_CHANNEL answers for it."""
        with attached_world(tmp_path) as world:
            assert _services(world).goals is None

    def test_a_queue_beside_a_planner_that_cannot_serve_goals_is_not_served(
        self, tmp_path: Path
    ) -> None:
        """Admitting goals nothing would ever activate is the forbidden port."""
        with attached_world(tmp_path) as world:
            world.loop.goals = GoalQueue(clock=world.clock, armed=False)
            world.loop.planner = _PlainPlanner()

            assert _services(world).goals is None

    def test_a_queue_beside_a_goal_planner_is_served_over_that_queue(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            world.loop.goals = GoalQueue(clock=world.clock, armed=False)
            world.loop.planner = _GoalCapablePlanner()
            services = _services(world)

            goals = services.goals
            assert isinstance(goals, LoopGoalPort)
            admission = goals.submit(
                GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="svc-key")
            )

            assert admission.goal is not None, "the queue's own admission crossed the port"
            assert goals.status(admission.goal.goal_id).named == admission.goal


class TestMemoryPort:
    def _memory(self, tmp_path: Path) -> SidecarMemory:
        return SidecarMemory(
            store=MemoryStore(tmp_path / "memory"), record_path=tmp_path / "mem-record.json"
        )

    def test_remembered_facts_arrive_with_their_kinds(self, tmp_path: Path) -> None:
        memory = self._memory(tmp_path)
        memory.follow("save-1")
        loaded = memory.loaded
        assert loaded is not None
        loaded.set_home(Square(x=10, y=20, z=0), now_ms=BASE_TIME_MS)
        loaded.reserve(full_type="Base.Axe", reason="chopping wood", now_ms=BASE_TIME_MS)
        port = LoopMemoryPort(memory)

        records = port.query(kinds=(), limit=10)

        assert [(r.kind, r.key) for r in records] == [
            ("home", "home"),
            ("reservation", "Base.Axe"),
        ]
        home = records[0]
        assert home.data == {"x": 10, "y": 20, "z": 0, "set_at_ms": BASE_TIME_MS}
        assert records[1].label == "chopping wood"

    def test_the_kind_filter_and_the_limit_are_applied(self, tmp_path: Path) -> None:
        memory = self._memory(tmp_path)
        memory.follow("save-1")
        loaded = memory.loaded
        assert loaded is not None
        loaded.set_home(Square(x=1, y=1, z=0), now_ms=BASE_TIME_MS)
        loaded.reserve(full_type="Base.Axe", reason="keep", now_ms=BASE_TIME_MS)
        loaded.reserve(full_type="Base.Saw", reason="keep", now_ms=BASE_TIME_MS)
        port = LoopMemoryPort(memory)

        only = port.query(kinds=("reservation",), limit=10)
        bounded = port.query(kinds=(), limit=1)

        assert {r.kind for r in only} == {"reservation"}
        assert len(only) == 2
        assert len(bounded) == 1

    def test_nothing_loaded_answers_empty_because_nothing_is_in_force(self, tmp_path: Path) -> None:
        assert LoopMemoryPort(self._memory(tmp_path)).query(kinds=(), limit=5) == ()

    def test_an_unreadable_memory_refuses_rather_than_forgetting(self, tmp_path: Path) -> None:
        memory = self._memory(tmp_path)
        broken = memory.store.path_for("save-2")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json", encoding="utf-8")
        memory.follow("save-2")

        with pytest.raises(LoopError, match="could not be read"):
            LoopMemoryPort(memory).query(kinds=(), limit=5)

    def test_a_loop_without_a_memory_refuses_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(LoopError, match="without a save memory"):
            LoopMemoryPort(None).query(kinds=(), limit=5)


class TestDiagnosticsPort:
    def _log(self, tmp_path: Path) -> Path:
        log = DiagnosticLog(tmp_path / "logs")
        assert log.info("attach.ok", detail="attached") is not None
        assert log.error("rpc.refused", code="E1", action_id="a-1") is not None
        return tmp_path / "logs" / "pz-agent.jsonl"

    def test_the_tail_is_the_log_s_own_records(self, tmp_path: Path) -> None:
        port = LoopDiagnosticsPort(doctor_source=_empty_doctor, log_file=self._log(tmp_path))

        records = port.tail(limit=10)

        assert [record.message for record in records] == ["attach.ok", "rpc.refused"]
        assert records[0].component == "attach"
        assert records[1].code == "E1"
        assert records[1].action_id == "a-1"

    def test_the_three_filters_select_and_none_means_no_filter(self, tmp_path: Path) -> None:
        port = LoopDiagnosticsPort(doctor_source=_empty_doctor, log_file=self._log(tmp_path))

        assert [r.message for r in port.tail(limit=10, level="ERROR")] == ["rpc.refused"]
        assert [r.message for r in port.tail(limit=10, component="attach")] == ["attach.ok"]
        assert [r.message for r in port.tail(limit=10, action_id="a-1")] == ["rpc.refused"]

    def test_a_missing_log_file_is_an_empty_tail(self, tmp_path: Path) -> None:
        port = LoopDiagnosticsPort(
            doctor_source=_empty_doctor, log_file=tmp_path / "logs" / "pz-agent.jsonl"
        )

        assert port.tail(limit=10) == ()

    def test_a_loop_with_nowhere_to_log_refuses_the_tail(self, tmp_path: Path) -> None:
        port = LoopDiagnosticsPort(doctor_source=_empty_doctor, log_file=None)

        with pytest.raises(LoopError, match="no diagnostic log"):
            port.tail(limit=10)

    def test_doctor_checks_keep_their_codes_and_verdicts(self, tmp_path: Path) -> None:
        def report() -> DoctorReport:
            return DoctorReport(
                checks=(
                    CheckResult(
                        code="PZD001",
                        name="game",
                        title="Game installation",
                        status=CheckStatus.PASS,
                        detail="found",
                    ),
                    CheckResult(
                        code="PZD003",
                        name="zomboid",
                        title="Zomboid directory",
                        status=CheckStatus.FAIL,
                        detail="missing",
                        remediation="run the game once",
                    ),
                ),
                generated_at="2026-08-08T00:00:00+00:00",
            )

        port = LoopDiagnosticsPort(doctor_source=report, log_file=None)

        checks = port.doctor()

        assert [(c.code, c.ok) for c in checks] == [("PZD001", True), ("PZD003", False)]
        assert checks[1].remediation == "run the game once"


def test_the_structured_log_round_trips_through_the_port(tmp_path: Path) -> None:
    """What the port reads is what the log wrote, byte-for-byte JSON lines."""
    log_dir = tmp_path / "logs"
    log = DiagnosticLog(log_dir)
    assert log.info("run.finished", ticks=2) is not None
    raw: list[dict[str, Any]] = [
        json.loads(line)
        for line in (log_dir / "pz-agent.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    port = LoopDiagnosticsPort(doctor_source=_empty_doctor, log_file=log_dir / "pz-agent.jsonl")

    records = port.tail(limit=5)

    assert len(records) == len(raw) == 1
    assert records[0].timestamp_ms == raw[0]["timestamp_ms"]
    assert records[0].level == raw[0]["level"]
