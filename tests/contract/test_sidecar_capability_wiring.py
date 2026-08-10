"""The capability gate, assembled the way ``pz-agent start`` assembles it.

Two subsystems meet here and neither one can see the seam from where it lives.

:class:`~pz_agent_core.actions.engine.ActionEngine` refuses any adapter that
names a ``required_capability`` until an injected
:data:`~pz_agent_core.actions.engine.CapabilityCheck` says otherwise, and its
default — :func:`~pz_agent_core.actions.engine.deny_capability` — answers False
to everything on purpose, so that "nobody wired a probe" fails closed instead of
assuming a game API exists. Every game adapter names one. So a sidecar assembled
without a check attaches, arms, and then declines every single action a user
asks for, forever, with a message about a capability nobody ever resolved.

No unit test can see that. The adapter suites and the engine suite each inject a
check of their own — ``lambda _name: True``, ``report.usable`` — because each is
about something else, and every one of them is green while the production
assembly passes nothing at all. The only place the defect exists is
:func:`pz_agent_cli.app.build_loop`, and the only way to catch it is to build a
loop with that function and drive an action through it.

That is what this file does. It builds the real loop over a temporary machine,
attaches, arms, and drives one ``survival.rest`` end to end against a fake mod
that writes observations and acks into the exchange directory. The assertion
that would have caught the original defect is one line:
``result.reason_code is not ReasonCode.CAPABILITY_UNAVAILABLE``.

The second half is the other direction of the same honesty rule. A static scan
may only ever reach ``available_unverified``; ``verified`` requires a live run,
and :func:`~pz_agent_core.capabilities.probes.confirm` is the only function that
can grant it. Wiring it has a trap that
``tests/contract/test_capability_evidence_agreement.py`` pins: the engine's own
``ActionResult`` wraps the observed values under ``observed``, while
``RuntimeConfirmation.missing_keys`` is a top-level exact match, so feeding it
the engine's document reports every declared key missing for every probe, in
silence. The run below reaching ``VERIFIED`` is what proves the right document
was handed over.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pz_agent_cli import app
from pz_agent_cli.context import Workspace, resolve_workspace
from pz_agent_cli.runtime import (
    CAPABILITY_FILE_NAME,
    LoopLimits,
    SidecarLoop,
    read_capability_record,
)
from pz_agent_core.actions.adapter import Evidence
from pz_agent_core.actions.engine import ActionRequest, deny_capability
from pz_agent_core.capabilities import load_report
from pz_agent_core.capabilities.probes import MOVE_TO_SQUARE, SURVIVAL_REST
from pz_agent_core.ipc.journal import JournalReader, JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CapabilityState,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from tests.fixtures import make_observation, make_player
from tests.fixtures.cli_worlds import CliWorld, make_world

BUILD: Final = "42.20"

#: Endurance before and after the rest. The gap has to straddle the target, or
#: the adapter refuses the command as already satisfied instead of running it.
TIRED: Final = 0.30
RESTED: Final = 0.85
TARGET: Final = 0.80

#: One tick, one action. The loop is driven a tick at a time rather than through
#: ``run``, so nothing here depends on a budget being spent to terminate.
LIMITS: Final = LoopLimits(
    tick_interval_ms=0,
    tick_budget=1,
    max_actions_per_window=2,
    action_window_ms=600_000,
    observations_per_tick=16,
    observation_window=8,
)


# ---------------------------------------------------------------------------
# the game side
# ---------------------------------------------------------------------------


@dataclass
class FakeMod:
    """The game half of the exchange directory, driven by the loop's own sleeper.

    Installed as the sidecar's :data:`~pz_agent_cli.runtime.Sleeper` because that
    is the only moment the loop yields: the engine waits for a fresh observation
    by sleeping, so a mod that writes one there answers exactly as often as it is
    asked — no thread, no wall-clock time, and every deadline in the engine still
    measured against a clock that only this object moves.

    It does what the mod does and in that order: refresh the heartbeat, carry out
    whatever was queued, then report the world as it now is. Raising endurance
    only once the command has been read is what makes the "after" observation
    proof of the action rather than of the fixture.
    """

    world: CliWorld
    layout: IpcLayout
    session_id: str
    monitor: HeartbeatMonitor
    endurance: float = TIRED
    seq: int = 0
    served: list[str] = field(default_factory=list)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: The mod's own Safety state, mirrored into the heartbeat. Load-bearing
    #: since the two-phase arm: the loop grants authority only on this mod's
    #: session.arm ack plus a heartbeat reporting the armed mode.
    armed: bool = False
    mode: SessionMode = SessionMode.OFF
    _ack_seq: int = 0
    _commands: JournalReader = field(init=False)

    def __post_init__(self) -> None:
        self._commands = JournalReader(self.layout, self.layout.command_queue)

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
        )

    def observe(self) -> Observation:
        self.seq += 1
        observation = make_observation(
            session_id=self.session_id,
            seq=self.seq,
            timestamp_ms=self.world.clock.now_ms,
            player=make_player(stats={"endurance": self.endurance}),
        )
        self._append(self.layout.observation_events, observation.to_dict())
        return observation

    def serve(self) -> None:
        """Run whatever the sidecar queued, then ack it."""
        for record in self._commands.read().records:
            command = Command.from_dict(record.payload)
            if command.action is ActionName.SESSION_ARM:
                self.armed = True
                self.mode = SessionMode(str(command.args.get("mode")))
                self._ack_session_control(command)
                continue
            if command.action is ActionName.SESSION_DISARM:
                self.armed = False
                self.mode = SessionMode.OFF
                self._ack_session_control(command)
                continue
            if command.action is not ActionName.SURVIVAL_REST:
                continue
            self.served.append(command.command_id)
            self.endurance = RESTED
            ack = ActionResult.succeeded(
                session_id=command.session_id,
                seq=self._next_ack_seq(),
                command_id=command.command_id,
                action=command.action.value,
                timestamp_ms=self.world.clock.now_ms,
                evidence={"endurance_before": TIRED, "endurance_after": RESTED},
            )
            self._append(self.layout.command_ack, ack.to_dict())

    def _next_ack_seq(self) -> int:
        seq = self._ack_seq
        self._ack_seq += 1
        return seq

    def _ack_session_control(self, command: Command) -> None:
        """The Arm/DisarmAdapters' own succeeded shape: observed before/after."""
        ack = ActionResult.succeeded(
            session_id=command.session_id,
            seq=self._next_ack_seq(),
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=self.world.clock.now_ms,
            evidence={"armed_after": self.armed, "mode_after": self.mode.value},
        )
        self._append(self.layout.command_ack, ack.to_dict())

    def pump(self, milliseconds: int) -> None:
        self.world.clock.advance(max(0, milliseconds))
        self.beat()
        self.serve()
        self.observe()

    def _append(self, path: Path, payload: JsonDict) -> None:
        writer = JournalWriter(self.layout, path)
        try:
            writer.append(payload)
        finally:
            writer.close()


@dataclass
class OneAction:
    """A planner that asks for one thing, once, and then has nothing to say.

    ``build_loop`` attaches no planner, so the loop it returns proposes nothing
    on its own. That is a separate gap from the one under test here; this stands
    in for it so the capability gate is reached by the same path a planner would
    reach it by — through ``SidecarLoop._act`` and the engine, not by calling the
    engine directly.
    """

    session_id: str
    proposed: int = 0

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.proposed += 1
        if self.proposed > 1:
            return None
        return ActionRequest(
            action=ActionName.SURVIVAL_REST,
            session_id=self.session_id,
            idempotency_key="capability-wiring-rest",
            args={"target_endurance": TARGET},
        )


@dataclass
class Assembled:
    """The real loop, the machine it was built over, and the mod answering it."""

    world: CliWorld
    workspace: Workspace
    loop: SidecarLoop
    mod: FakeMod
    planner: OneAction

    def __enter__(self) -> Assembled:
        return self

    def __exit__(self, *exc: object) -> None:
        # ``filterwarnings = ["error"]`` turns the ResourceWarning from an
        # unclosed journal into a failure, which is the signal wanted.
        if self.loop.session is not None:
            self.loop.shutdown(reason="the test finished")


def assemble(world: CliWorld) -> Assembled:
    """Build the loop the way ``pz-agent start --foreground`` does, and arm it."""
    workspace = resolve_workspace(world.ctx)
    # Frozen so that the mod answering is the only thing that moves time: every
    # engine deadline and every heartbeat age then means exactly what it says.
    world.clock.freeze()
    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)
    assert workspace.ipc_root is not None
    layout = IpcLayout(workspace.ipc_root)
    mod = FakeMod(
        world=world,
        layout=layout,
        # The mod reads the session id out of session.json, which does not exist
        # until the sidecar attaches a few lines below.
        session_id="",
        monitor=HeartbeatMonitor(layout, clock=world.ctx.clock_ms),
    )
    # Before attaching: the observation source captures the sleeper when the
    # session is built, and it is the seam the fake mod answers through.
    loop.sleep = mod.pump

    attach = loop.attach()
    assert attach.attached, attach.detail
    session = loop.session
    assert session is not None
    mod.session_id = session.session_id
    mod.beat()
    armed = loop.arm(SessionMode.ASSISTED)
    assert armed.pending, armed.detail
    # Two-phase: the mod executes session.arm, acks it, and its next heartbeat
    # reports the armed mode; the following tick observes both and grants. The
    # planner is attached only after that, so the confirmation tick cannot
    # spend its one proposal.
    mod.pump(0)
    mod.beat()
    granted = loop.tick()
    assert granted.armed, (
        armed.detail if loop.arm_resolution is None else loop.arm_resolution.detail
    )
    planner = OneAction(session_id=session.session_id)
    loop.planner = planner
    mod.observe()
    return Assembled(world=world, workspace=workspace, loop=loop, mod=mod, planner=planner)


def engine_shaped_result(action: ActionName, observed: JsonDict) -> ActionResult:
    """A sidecar ``ActionResult`` in the shape the engine really produces.

    Built through the adapter's own :class:`Evidence` rather than by hand, so it
    carries the nesting under ``observed`` that the confirmation wiring has to
    undo. Writing the flat bag here instead would test the fixture.
    """
    return ActionResult.succeeded(
        session_id=str(uuid.UUID(int=0x5E5510)),
        seq=1,
        command_id=str(uuid.UUID(int=0xC0FFEE)),
        action=action.value,
        timestamp_ms=1_700_000_000_000,
        evidence=Evidence(
            kind="endurance_recovered", observation_seq=3, observed=observed
        ).to_payload(),
    )


# ---------------------------------------------------------------------------
# the static check reaches the assembled loop
# ---------------------------------------------------------------------------


def test_build_loop_wires_a_real_capability_check_rather_than_the_closed_default(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    assert loop.capability_check is not deny_capability
    assert loop.capability_check(SURVIVAL_REST) is True
    assert loop.capabilities is not None
    assert loop.capabilities.resolved


def test_the_check_answers_for_what_the_local_scan_found_and_refuses_the_rest(
    tmp_path: Path,
) -> None:
    """Not a blanket yes. The scan's verdict is what the engine is gated on."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    assert loop.capability_check(MOVE_TO_SQUARE) is True
    # Refused by design (§12.4) rather than by a missing probe run, and the wired
    # check has to keep saying so.
    assert loop.capability_check("autonomous_attack") is False
    assert loop.capability_check("no_such_capability") is False


def test_one_action_runs_through_the_assembled_loop_and_is_not_refused_for_capability(
    tmp_path: Path,
) -> None:
    """The assertion that would have caught the original defect.

    Everything else in this file describes the wiring; this drives it. A loop
    built by ``build_loop`` with no capability check refuses this action before
    the command is composed, and the user sees ``CAPABILITY_UNAVAILABLE`` for
    every action they ever ask for.
    """
    with assemble(make_world(tmp_path)) as assembled:
        outcome = assembled.loop.tick()

        assert len(outcome.results) == 1
        result = outcome.results[0]
        assert result.reason_code is not ReasonCode.CAPABILITY_UNAVAILABLE, (
            f"the assembled sidecar refused survival.rest as an unusable capability "
            f"({result.message}); the static scan found it on this install, so nothing "
            "wired the check into the loop"
        )
        assert result.status is ActionStatus.SUCCEEDED, result.message
        assert assembled.mod.served, "the command never reached the mod"


# ---------------------------------------------------------------------------
# a live run is the only thing that verifies anything
# ---------------------------------------------------------------------------


def test_a_succeeded_run_promotes_its_capability_to_verified_and_persists_it(
    tmp_path: Path,
) -> None:
    with assemble(make_world(tmp_path)) as assembled:
        before = assembled.loop.capabilities
        assert before is not None and before.report is not None
        assert before.report.state(SURVIVAL_REST) is CapabilityState.AVAILABLE_UNVERIFIED

        assembled.loop.tick()

        ledger = assembled.loop.capabilities
        assert ledger is not None and ledger.report is not None
        capability = ledger.report.get(SURVIVAL_REST)
        assert capability is not None
        assert capability.state is CapabilityState.VERIFIED, capability.reason
        assert capability.has_runtime_evidence

        # On disk, so it outlives the process that observed it.
        loaded = load_report(assembled.workspace.capability_report_path, expected_build=BUILD)
        assert loaded.report.state(SURVIVAL_REST) is CapabilityState.VERIFIED
        record = read_capability_record(assembled.workspace.state_dir / CAPABILITY_FILE_NAME)
        assert record is not None
        assert SURVIVAL_REST in record.verified


def test_a_new_session_does_not_inherit_last_run_s_verified_claim(tmp_path: Path) -> None:
    """§3.8: the report holds probe *results*, not promises.

    The claim is written down so it survives the process, and it is still not
    carried into the next session: the next sidecar resolves the install again
    from symbols, which can never yield ``verified``, and has to re-earn it by
    running the action again.
    """
    world = make_world(tmp_path)
    with assemble(world) as assembled:
        assembled.loop.tick()
        workspace = assembled.workspace
        assert (
            load_report(workspace.capability_report_path, expected_build=BUILD).report.state(
                SURVIVAL_REST
            )
            is CapabilityState.VERIFIED
        )

    ledger = app.build_capabilities(workspace)

    assert ledger.report is not None
    assert ledger.report.state(SURVIVAL_REST) is CapabilityState.AVAILABLE_UNVERIFIED
    assert ledger.record().verified == ()


def test_an_ack_missing_a_declared_evidence_key_promotes_nothing_and_names_it(
    tmp_path: Path,
) -> None:
    """The probe's own gate, reached through the wiring rather than around it."""
    world = make_world(tmp_path)
    ledger = app.build_capabilities(resolve_workspace(world.ctx))
    incomplete = engine_shaped_result(ActionName.SURVIVAL_REST, {"endurance_before": TIRED})

    confirmed = ledger.confirm_capability(SURVIVAL_REST, ActionName.SURVIVAL_REST, incomplete)

    assert confirmed is not None
    assert confirmed.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert "endurance_after" in confirmed.reason
    assert ledger.record().verified == ()


def test_a_result_that_did_not_succeed_promotes_nothing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    ledger = app.build_capabilities(resolve_workspace(world.ctx))
    failed = ActionResult.failure(
        session_id=str(uuid.UUID(int=0x5E5510)),
        seq=1,
        command_id=str(uuid.UUID(int=0xC0FFEE)),
        action=ActionName.SURVIVAL_REST.value,
        timestamp_ms=1_700_000_000_000,
        reason_code=ReasonCode.ACTION_TIMEOUT,
        evidence={"endurance_before": TIRED, "endurance_after": RESTED},
    )

    assert ledger.confirm_capability(SURVIVAL_REST, ActionName.SURVIVAL_REST, failed) is None
    assert ledger.record().verified == ()


def test_an_ack_for_another_action_neither_promotes_nor_destroys_a_claim(
    tmp_path: Path,
) -> None:
    """Several adapters share one capability; only one of them can confirm it.

    ``movement.move_near`` requires ``move_to_square``, whose probe confirms on
    ``movement.move_to``. Handing its ack to ``confirm`` would be handing over an
    ack for the wrong action — which is refused, and the refusal *demotes*. A
    successful sidestep would then destroy a verification a real walk earned.
    """
    world = make_world(tmp_path)
    ledger = app.build_capabilities(resolve_workspace(world.ctx))
    assert ledger.report is not None
    walked = ledger.report.get(MOVE_TO_SQUARE)
    sidestep = engine_shaped_result(ActionName.MOVEMENT_MOVE_NEAR, {"position": {"x": 1, "y": 2}})

    assert (
        ledger.confirm_capability(MOVE_TO_SQUARE, ActionName.MOVEMENT_MOVE_NEAR, sidestep) is None
    )
    assert ledger.report.get(MOVE_TO_SQUARE) == walked


# ---------------------------------------------------------------------------
# a scan that cannot run
# ---------------------------------------------------------------------------


def test_a_failed_scan_refuses_every_gated_action_instead_of_allowing_them(
    tmp_path: Path,
) -> None:
    """An install with no ``media/lua`` to read. Nothing may be assumed from it."""
    world = make_world(tmp_path, with_lua=False)

    with assemble(world) as assembled:
        ledger = assembled.loop.capabilities
        assert ledger is not None
        assert not ledger.resolved
        assert assembled.loop.capability_check(SURVIVAL_REST) is False

        outcome = assembled.loop.tick()

        assert len(outcome.results) == 1
        assert outcome.results[0].reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert assembled.mod.served == [], "a command reached the mod for an unresolved capability"


def test_a_failed_scan_says_so_in_status_rather_than_only_in_the_refusals(
    tmp_path: Path,
) -> None:
    """The refusal alone would be a mystery: the user has to be able to see why."""
    world = make_world(tmp_path, with_lua=False)
    with assemble(world):
        pass

    world.reset_streams()
    assert world.run("status") == 0

    printed = world.stdout
    assert "NOT RESOLVED" in printed
    assert "could not be scanned" in printed
    record = read_capability_record(resolve_workspace(world.ctx).state_dir / CAPABILITY_FILE_NAME)
    assert record is not None
    assert record.resolved is False
    assert "the capability scan could not run" in record.detail


def test_status_reports_a_resolved_surface_when_the_scan_did_run(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    with assemble(world) as assembled:
        assembled.loop.tick()

    world.reset_streams()
    assert world.run("status", "--json") == 0

    printed = world.stdout
    assert "NOT RESOLVED" not in printed
    assert SURVIVAL_REST in printed


def test_a_state_directory_no_sidecar_has_touched_says_so_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """Absent is not the same answer as unresolved, and must not read like one."""
    world = make_world(tmp_path)

    assert world.run("status") == 0

    assert "no sidecar has resolved them" in world.stdout
