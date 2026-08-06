"""The memory seam, assembled the way ``pz-agent start`` assembles it.

``AutonomyPlanner`` held ``memory: AutonomyMemory = NO_MEMORY`` and nothing ever
passed a real one, so ``reserves_item`` answered False to every question a live
session could ask. §7.9 — do not eat what the user set aside — rested entirely on
the selection policies' own tag rules, and the user had no way to set anything
aside at all: no CLI command and no MCP tool wrote memory. ``home_point()``
answered None for the same reason, so every plan that travels was refused for
want of a home point that nothing could record.

The memory package was finished and separately tested throughout.
:class:`~pz_agent_core.memory.store.SaveMemory` satisfies the autonomy gate's
protocol member for member and
:class:`~pz_agent_core.memory.persistence.MemoryStore` already loaded and saved
one per save id. What was missing was between them and the loop, and no unit
test could see it: the store's suite is green against a store nobody consults,
and the gate's suite is green against a memory a fixture supplies.

The first test here is the one line that catches it —
``isinstance(loop.planner.memory, SidecarMemory)`` and a real store behind it
once a tick has named a save. The one that *matters* is
:func:`test_an_item_the_user_reserved_is_not_fed_to_the_hungry_character`: the
promise §7.9 makes to the user, driven end to end through the assembled loop and
the real ``pz-agent remember reserve``.

Loading is lazy because it cannot be anything else. A memory is scoped to a save
and the save is the mod's to name, so the loop is built before the id it needs
exists. Until an observation carries one the planner sees an empty memory, which
reserves nothing and knows no home — permissive about spending and refusing about
travelling, exactly as :class:`~pz_agent_core.policy.autonomy.NoMemory`
documents. A save id that changes mid-session is a different world and the loaded
memory is dropped rather than carried into it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final

from pz_agent_cli import app
from pz_agent_cli.autonomy import AutonomyPlanner
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, Workspace, resolve_workspace
from pz_agent_cli.memory import (
    MEMORY_RECORD_NAME,
    SidecarMemory,
    memory_store,
    read_memory_record,
)
from pz_agent_cli.runtime import LoopLimits, SidecarLoop
from pz_agent_core.actions.adapter import Evidence
from pz_agent_core.capabilities.probes import EAT_PERCENTAGE, MOVE_TO_SQUARE, probe_for
from pz_agent_core.ipc.journal import JournalReader, JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.snapshot import SnapshotWriter
from pz_agent_core.memory import Square
from pz_agent_core.planner import (
    MoveToArgs,
    Plan,
    PlanProposal,
    PlanRequest,
    PlanStep,
    SuccessCriterion,
    SuccessKind,
)
from pz_agent_core.policy.autonomy import AutonomyOutcome, BackupEvidence
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CapabilityState,
    Command,
    ContainerKind,
    DangerLevel,
    InventoryView,
    ItemView,
    JsonDict,
    NearbyView,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from tests.fixtures import make_container, make_game, make_observation, make_player, make_safety
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.policy_items import food_payload

BUILD: Final = "42.20"

#: Above the §17.1 hunger trigger, so there is a real need every tick and the
#: only thing that changes between these cases is what memory answers.
HUNGRY: Final = 0.60
FED: Final = 0.05
CALM: Final = 0.10

#: The only food in this world, so reserving it leaves nothing else to choose.
BEANS: Final = "Base.TinnedBeans"

#: Two saves. The mod reports a digest of a key this side never sees, so these
#: are opaque strings on purpose — a memory is scoped by one and nothing else.
SAVE: Final = "1f3c9a2b7e40d115"
OTHER_SAVE: Final = "9911aabb00cc22dd"

#: Where the character stands in every observation these tests publish.
HOME: Final = Square(x=1200, y=3400, z=0)

#: Ten squares from :data:`HOME`: inside the movement adapter's own P2 radius, so
#: the step is not refused as P3 before §7.10 is ever consulted, and inside the
#: 30-square home radius, so a *set* home point approves what an unset one
#: refuses.
NEARBY_TARGET: Final = (HOME.x + 10, HOME.y)

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


def main_ref(session_id: str) -> str:
    return f"container:{session_id}:player-main"


def item_in(container_ref: str, runtime_id: str) -> str:
    tail = container_ref.split(":", 2)[2]
    return f"item:{container_ref.split(':')[1]}:{tail}:{runtime_id}:0"


def tinned_beans(container_ref: str) -> ItemView:
    """A tin the food policy accepts on every count except being reserved."""
    return ItemView(
        ref=item_in(container_ref, "beans-1"),
        container_ref=container_ref,
        full_type=BEANS,
        display_name="Tinned Beans",
        category="Food",
        weight=0.8,
        food=food_payload(),
    )


@dataclass
class FakeMod:
    """The game half of the exchange directory, driven by the loop's own sleeper.

    Publishes each observation twice, exactly as the mod does: onto the journal
    the loop reads, and into the alternating snapshot slots with the pointer
    written last. The second one is not decoration here — it is the only thing
    ``pz-agent remember`` can read the save id and the character's square from,
    and a test that wrote only the journal would be testing a mod that does not
    exist.
    """

    world: CliWorld
    layout: IpcLayout
    session_id: str
    monitor: HeartbeatMonitor
    save_id: str = SAVE
    hunger: float = CALM
    mode: SessionMode = SessionMode.AUTONOMOUS
    armed: bool = True
    seq: int = 0
    served: list[str] = field(default_factory=list)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    _commands: JournalReader = field(init=False)
    _snapshots: SnapshotWriter = field(init=False)

    def __post_init__(self) -> None:
        self._commands = JournalReader(self.layout, self.layout.command_queue)
        self._snapshots = SnapshotWriter(self.layout, clock=lambda: self.world.clock.now_ms)

    def beat(self) -> None:
        self.monitor.publish(
            Peer.GAME,
            session_id=self.session_id,
            nonce=self.nonce,
            version=PRODUCT_VERSION,
            build=BUILD,
            player_present=True,
        )

    def inventory(self) -> InventoryView:
        main = main_ref(self.session_id)
        return InventoryView(
            containers=[make_container(main, ContainerKind.PLAYER_MAIN, name="Inventory")],
            items=[tinned_beans(main)],
        )

    def observe(self) -> Observation:
        self.seq += 1
        observation = make_observation(
            session_id=self.session_id,
            seq=self.seq,
            timestamp_ms=self.world.clock.now_ms,
            game=make_game(save_id=self.save_id, build=BUILD),
            player=make_player(
                stats={"hunger": self.hunger, "thirst": CALM, "fatigue": CALM, "endurance": 0.9},
            ),
            safety=make_safety(armed=self.armed, mode=self.mode, danger_level=DangerLevel.NONE),
            inventory=self.inventory(),
            nearby=NearbyView(),
        )
        document = observation.to_dict()
        self._append(self.layout.observation_events, document)
        self._snapshots.publish(document)
        return observation

    def serve(self) -> None:
        """Run whatever the sidecar queued, then ack it."""
        for record in self._commands.read().records:
            command = Command.from_dict(record.payload)
            if command.action is not ActionName.CONSUME_EAT:
                continue
            self.hunger = FED
            self.served.append(command.action.value)
            self._append(
                self.layout.command_ack,
                ActionResult.succeeded(
                    session_id=command.session_id,
                    seq=len(self.served),
                    command_id=command.command_id,
                    action=command.action.value,
                    timestamp_ms=self.world.clock.now_ms,
                    evidence={"queued": True},
                ).to_dict(),
            )

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
class WalkAwayProvider:
    """A provider whose first step travels, so §7.10's radius check is reached.

    None of the three needs this build plans for moves the character, so the
    home point would otherwise never be consulted by anything the assembled loop
    runs. Standing a provider in here is the smallest way to reach the check
    with everything else — the gate, the critic, the memory, the loop — being the
    real assembly.
    """

    name: ClassVar[str] = "walk_away"

    def propose(self, request: PlanRequest) -> PlanProposal:
        step = PlanStep(
            step_id="walk",
            action=ActionName.MOVEMENT_MOVE_TO,
            args=MoveToArgs(x=NEARBY_TARGET[0], y=NEARBY_TARGET[1], z=HOME.z),
            success=SuccessCriterion(kind=SuccessKind.POSITION_REACHED),
        )
        return PlanProposal.proposed(
            Plan(goal_id=request.goal.goal_id, summary="walk a little way", steps=(step,)),
            "a step that travels, so the home radius is what decides it",
        )


def witness_for_the_observed_save(save_id: str) -> BackupEvidence | None:
    """A backup this test can attribute to whatever save the mod reports.

    The production witness answers None for every save on a machine with no
    attributed backup, and §7.7 then puts the whole autonomous path behind an
    ask. ``tests/contract/test_backup_attribution.py`` owns that rule; standing
    in for it here is what lets the memory seam be driven at all.
    """
    return BackupEvidence(save_id=save_id, created_at_ms=1_700_000_000_000, verified=True)


def prove_capability(loop: SidecarLoop, *capabilities: str) -> None:
    """Promote each capability to ``verified`` the way a live run does.

    §7.7 will not let the agent try an ``available_unverified`` capability
    unattended and a static scan can never produce anything better, so without
    this every case below would refuse for a reason that has nothing to do with
    memory. The ledger's own confirmation path is used rather than the report
    being edited, so the ladder is exercised and not relaxed.
    """
    ledger = loop.capabilities
    assert ledger is not None
    for capability in capabilities:
        probe = probe_for(capability)
        action = probe.confirmation.action
        observed: JsonDict = {key: 1.0 for key in probe.confirmation.evidence_keys}
        confirmed = ledger.confirm_capability(
            capability,
            action,
            ActionResult.succeeded(
                session_id=str(uuid.UUID(int=0x5E5510)),
                seq=1,
                command_id=str(uuid.UUID(int=0xC0FFEE)),
                action=action.value,
                timestamp_ms=1_700_000_000_000,
                evidence=Evidence(
                    kind="proven_by_a_run", observation_seq=1, observed=observed
                ).to_payload(),
            ),
        )
        assert confirmed is not None and confirmed.state is CapabilityState.VERIFIED, confirmed


@dataclass
class Assembled:
    """The real loop, the machine it was built over, and the mod answering it."""

    world: CliWorld
    workspace: Workspace
    loop: SidecarLoop
    mod: FakeMod

    @property
    def planner(self) -> AutonomyPlanner:
        planner = self.loop.planner
        assert isinstance(planner, AutonomyPlanner), planner
        return planner

    @property
    def memory(self) -> SidecarMemory:
        memory = self.planner.memory
        assert isinstance(memory, SidecarMemory), memory
        return memory

    def run(self, *argv: str) -> int:
        """Run a real CLI command against the same machine, streams reset."""
        self.world.reset_streams()
        return self.world.run(*argv)

    def __enter__(self) -> Assembled:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.loop.session is not None:
            self.loop.shutdown(reason="the test finished")


def assemble(world: CliWorld, **mod_state: object) -> Assembled:
    """Build the loop the way ``pz-agent start --foreground`` does, and arm it."""
    workspace = resolve_workspace(world.ctx)
    world.clock.freeze()
    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)
    assert workspace.ipc_root is not None
    layout = IpcLayout(workspace.ipc_root)
    mod = FakeMod(
        world=world,
        layout=layout,
        session_id="",
        monitor=HeartbeatMonitor(layout, clock=world.ctx.clock_ms),
        **mod_state,  # type: ignore[arg-type]
    )
    loop.sleep = mod.pump

    attach = loop.attach()
    assert attach.attached, attach.detail
    session = loop.session
    assert session is not None
    mod.session_id = session.session_id
    mod.beat()
    planner = loop.planner
    assert isinstance(planner, AutonomyPlanner)
    planner.backup = witness_for_the_observed_save
    armed = loop.arm(SessionMode.AUTONOMOUS)
    assert armed.armed, armed.detail
    mod.observe()
    return Assembled(world=world, workspace=workspace, loop=loop, mod=mod)


# ---------------------------------------------------------------------------
# the memory reaches the planner
# ---------------------------------------------------------------------------


def test_build_loop_wires_a_memory_rather_than_leaving_reservations_unread(
    tmp_path: Path,
) -> None:
    """The assertion that would have caught the original defect.

    Nothing is loaded at this point and nothing could be — no observation has
    named a save — but *what would load one* is either there or it is not, and
    it was not.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    planner = loop.planner
    assert isinstance(planner, AutonomyPlanner)
    assert isinstance(planner.memory, SidecarMemory), (
        "build_loop assembled a planner with no store behind its memory: it answers "
        "'nothing is reserved' to every question, so §7.9 protects nothing"
    )
    assert planner.memory.loaded is None
    assert planner.memory.home_point() is None
    assert planner.memory.reserves_item(BEANS) is False


def test_the_planner_has_a_real_memory_once_an_observation_names_a_save(
    tmp_path: Path,
) -> None:
    """Lazy, and not optional: the save id only exists once the mod has spoken."""
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        assert assembled.memory.record().loaded is False

        assembled.loop.tick()

        loaded = assembled.memory.loaded
        assert loaded is not None, assembled.memory.detail
        assert loaded.save_id == SAVE


def test_the_memory_record_says_what_the_assembled_loop_is_really_on(tmp_path: Path) -> None:
    """Written from the loop, so ``status`` cannot disagree with what is in force."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    app.build_loop(world.ctx, workspace, limits=LIMITS)

    record = read_memory_record(workspace.state_dir / MEMORY_RECORD_NAME)
    assert record is not None
    assert record.wired is True
    assert record.loaded is False
    assert record.readable is True


# ---------------------------------------------------------------------------
# §7.9, end to end
# ---------------------------------------------------------------------------


def test_an_item_the_user_reserved_is_not_fed_to_the_hungry_character(tmp_path: Path) -> None:
    """The test this file exists for: the promise §7.9 makes to the user.

    The tin is the only food in the world and the character is hungry enough for
    the gate to want it. What stops the meal is one command the user ran, read
    back through the store, the seam and the gate.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)
        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK, assembled.world.stderr

        outcome = assembled.loop.tick()

        assert outcome.results == (), assembled.planner.last_detail
        assert assembled.mod.served == [], "the agent ate what the user set aside"
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.outcome is AutonomyOutcome.ASK_USER
        assert decision.reason_code is ReasonCode.RESOURCE_RESERVED


def test_the_same_character_is_fed_when_that_reservation_is_released(tmp_path: Path) -> None:
    """The other half, so it is the reservation that made the difference."""
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)
        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK
        assert assembled.run("remember", "release", BEANS) == EXIT_OK, assembled.world.stderr

        outcome = assembled.loop.tick()

        assert [result.action for result in outcome.results] == [ActionName.CONSUME_EAT.value], (
            assembled.planner.last_detail
        )
        assert outcome.results[0].status is ActionStatus.SUCCEEDED
        assert assembled.mod.served == [ActionName.CONSUME_EAT.value]


def test_a_reservation_made_while_the_loop_runs_takes_effect_on_the_next_tick(
    tmp_path: Path,
) -> None:
    """A promise kept late is a promise broken once.

    The memory is already loaded here — a tick has run and found nothing
    reserved — so this is the case where the seam has to notice that another
    process wrote the file, rather than answering from what it read at start-up.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)
        assembled.loop.tick()
        assert assembled.mod.served == [ActionName.CONSUME_EAT.value]
        assembled.mod.hunger = HUNGRY
        assembled.mod.observe()

        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK, assembled.world.stderr
        assembled.loop.tick()

        assert assembled.mod.served == [ActionName.CONSUME_EAT.value], (
            "the second meal was served from a memory loaded before the reservation existed"
        )
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.reason_code is ReasonCode.RESOURCE_RESERVED


# ---------------------------------------------------------------------------
# §7.10: the home point
# ---------------------------------------------------------------------------


def test_a_home_point_set_through_the_cli_is_the_one_autonomy_sees(tmp_path: Path) -> None:
    """From ``remember home`` to the critic's radius check, through the real loop.

    Before the command there is no home point, and a step that travels is
    refused for exactly that reason. After it the same step is inside the
    radius, so the critic approves and the plan is stopped one gate later — by
    the bridge's own rule that a plan naming something other than the item the
    selection policy chose is not something to run unasked. Two different
    refusals, and the difference between them is the square the user recorded.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE, MOVE_TO_SQUARE)
        assembled.planner.provider = WalkAwayProvider()

        assembled.loop.tick()
        assert "no home point is set" in assembled.planner.last_detail

        assert assembled.run("remember", "home") == EXIT_OK, assembled.world.stderr
        assembled.mod.observe()
        assembled.loop.tick()

        assert assembled.memory.home_point() == HOME
        detail = assembled.planner.last_detail
        assert "no home point is set" not in detail, detail
        assert "the selection policy did not choose" in detail, detail


def test_remember_home_with_no_session_attached_changes_nothing_and_says_so(
    tmp_path: Path,
) -> None:
    """No save id, no memory: there is nothing to scope one to, so nothing is written."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    exit_code = world.run("remember", "home")

    assert exit_code == EXIT_FAILURE
    assert "no session is attached" in world.stderr
    assert "Nothing was changed" in world.stderr
    root = workspace.state_dir / "memory"
    assert not root.exists(), "a command that refused still wrote a memory file"


# ---------------------------------------------------------------------------
# one save's memory is not another's
# ---------------------------------------------------------------------------


def test_a_different_save_does_not_inherit_the_previous_saves_reservations(
    tmp_path: Path,
) -> None:
    """A save id that changes is a different world, not a renamed one.

    The reflex guard sees it first and disarms — every reference the loop held
    names something in a world that is no longer loaded — so the tick that
    matters here is the one after the user re-arms. What must not have survived
    that is the previous save's reservation, and the proof is that the tin the
    other world's user set aside is eaten in this one.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)
        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK
        assembled.loop.tick()
        assert assembled.mod.served == []

        assembled.mod.save_id = OTHER_SAVE
        assembled.mod.observe()
        disarming = assembled.loop.tick()
        assert [event.reason_code for event in disarming.events] == [ReasonCode.SAVE_CHANGED]
        rearmed = assembled.loop.arm(SessionMode.AUTONOMOUS)
        assert rearmed.armed, rearmed.detail
        assembled.mod.observe()
        assembled.loop.tick()

        loaded = assembled.memory.loaded
        assert loaded is not None and loaded.save_id == OTHER_SAVE
        assert assembled.memory.reserves_item(BEANS) is False
        assert assembled.mod.served == [ActionName.CONSUME_EAT.value], assembled.planner.last_detail


def test_memory_survives_a_restart_of_the_loop(tmp_path: Path) -> None:
    """The point of a store: what the user said outlives the process that read it."""
    world = make_world(tmp_path)
    with assemble(world, hunger=HUNGRY) as first:
        assert first.run("remember", "reserve", BEANS) == EXIT_OK
        assert first.run("remember", "home") == EXIT_OK

    with assemble(world, hunger=HUNGRY) as second:
        prove_capability(second.loop, EAT_PERCENTAGE)
        second.loop.tick()

        assert second.memory.reserves_item(BEANS) is True
        assert second.memory.home_point() == HOME
        assert second.mod.served == []


# ---------------------------------------------------------------------------
# what status says about it
# ---------------------------------------------------------------------------


def test_status_says_a_loaded_memory_is_in_force_and_what_is_in_it(tmp_path: Path) -> None:
    """A reservation nobody can see honoured is the defect this file guards."""
    world = make_world(tmp_path)
    with assemble(world, hunger=HUNGRY) as assembled:
        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK
        assembled.loop.tick()

        assert assembled.run("status") == EXIT_OK
        printed = assembled.world.stdout

    assert "loaded — 1 reservation(s) and no home point" in printed, printed


def test_status_distinguishes_a_memory_that_has_loaded_nothing_yet(tmp_path: Path) -> None:
    """Not loaded and nothing reserved answer alike from outside, and must not read alike."""
    world = make_world(tmp_path)
    app.build_loop(world.ctx, resolve_workspace(world.ctx), limits=LIMITS)

    world.reset_streams()
    assert world.run("status") == EXIT_OK

    assert "not loaded — no observation has named a save yet" in world.stdout


def test_a_state_directory_no_sidecar_has_run_in_says_so(tmp_path: Path) -> None:
    """Absent is not the same answer as unwired, and must not read like one."""
    world = make_world(tmp_path)

    assert world.run("status") == EXIT_OK

    assert "no sidecar has loaded one in this state directory" in world.stdout


# ---------------------------------------------------------------------------
# the store the commands and the sidecar share
# ---------------------------------------------------------------------------


def test_the_cli_and_the_sidecar_read_one_store(tmp_path: Path) -> None:
    """One root, so a reservation cannot be written where nothing looks for it."""
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        assert assembled.run("remember", "reserve", BEANS) == EXIT_OK
        assembled.loop.tick()

        loaded = assembled.memory.loaded
        assert loaded is not None
        written = memory_store(assembled.workspace).path_for(SAVE)
        assert written.is_file()
        assert [record.full_type for record in loaded.reservations()] == [BEANS]
