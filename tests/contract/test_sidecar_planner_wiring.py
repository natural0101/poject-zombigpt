"""The planner seam, assembled the way ``pz-agent start`` assembles it.

``SidecarLoop._act`` opens with ``if self.planner is None or not self._armed:
return ()``. Everything after that line — the budget, the engine, the capability
gate, the confirmation of a probe by a live run — is unreachable when nothing
passed a planner in. :func:`pz_agent_cli.app.build_loop` passed none, so
``pz-agent arm --mode autonomous`` produced a sidecar that attached, observed,
ran the reflex guard, refreshed its heartbeat, and never once proposed
anything. Autonomous mode was inert, and it was inert *quietly*: a loop with no
planner and a loop with nothing to do behave identically from outside.

No unit test can see that. The autonomy gate's suite, the provider's suite, the
critic's suite and the loop's own suite are all green against the assembled
sidecar being mute, because each one supplies the piece the others are missing.
The only place the defect exists is ``build_loop``, and the only way to catch it
is to build a loop with that function and look at what came back.

That is the first test here, and it is one line:
``assert loop.planner is not None``. The rest drive the assembled loop against a
fake mod: a hungry character with a safe tin in reach is fed, a calm one is left
alone, an unarmed one is never even consulted, and a threat the reflex guard is
not yet acting on still stops a long action being started.

Two gates in the autonomous path are *not* satisfied by a fresh install, and
both are tested here as the refusals they are rather than being configured away:

* **§7.7 refuses an unverified capability on the agent's own initiative.** A
  static scan can only ever reach ``available_unverified``; a live run is what
  promotes it. So the first autonomous meal is refused until some user-directed
  run has proven ``eat_percentage`` against this build, which is the ladder
  working, not a defect.
* **§7.7 refuses an unbacked save**, and this build cannot attribute a local
  backup to the save the mod reports — the two are named in different identifier
  spaces. ``build_loop`` therefore passes
  :func:`~pz_agent_cli.autonomy.no_attributable_backup`, autonomy asks instead of
  acting, and ``pz-agent status`` carries the reason. The tests that need a
  proposal replace that one witness and nothing else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pz_agent_cli import app
from pz_agent_cli.autonomy import (
    BACKUP_NOT_ATTRIBUTABLE,
    PLANNER_FILE_NAME,
    AutonomyPlanner,
    read_planner_record,
)
from pz_agent_cli.context import Workspace, resolve_workspace
from pz_agent_cli.runtime import LoopLimits, SidecarLoop
from pz_agent_core.actions.adapter import Evidence
from pz_agent_core.capabilities.probes import (
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    READ_LITERATURE,
    probe_for,
)
from pz_agent_core.ipc.journal import JournalReader, JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.planner import NullProvider
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
from tests.fixtures import make_container, make_observation, make_player, make_safety
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.policy_items import food_payload, literature_payload

BUILD: Final = "42.20"

#: Hunger before and after the meal. The first is above the policy's trigger and
#: the second below it, so the need is real on the tick it is served and the
#: adapter has a fall in the stat to verify against.
HUNGRY: Final = 0.60
FED: Final = 0.05

#: A character with nothing on the §17.1 table above its trigger.
CALM: Final = 0.10

#: Level the mod reports the boredom moodle at. Above
#: ``PolicyConfig.boredom_moodle_trigger``, so reading is a real need.
BORED: Final = 3

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


def backpack_ref(session_id: str) -> str:
    return f"container:{session_id}:worn:Back:99001"


def item_in(container_ref: str, runtime_id: str) -> str:
    tail = container_ref.split(":", 2)[2]
    return f"item:{container_ref.split(':')[1]}:{tail}:{runtime_id}:0"


def tinned_beans(container_ref: str) -> ItemView:
    """A tin the food policy accepts: edible, fresh, filling, nobody's reserve."""
    return ItemView(
        ref=item_in(container_ref, "beans-1"),
        container_ref=container_ref,
        full_type="Base.TinnedBeans",
        display_name="Tinned Beans",
        category="Food",
        weight=0.8,
        food=food_payload(),
    )


def paperback(container_ref: str) -> ItemView:
    """Something to read, for the one long action this build can plan."""
    return ItemView(
        ref=item_in(container_ref, "book-1"),
        container_ref=container_ref,
        full_type="Base.Book",
        display_name="Paperback",
        category="Literature",
        weight=0.4,
        literature=literature_payload(),
    )


@dataclass
class FakeMod:
    """The game half of the exchange directory, driven by the loop's own sleeper.

    Installed as the sidecar's :data:`~pz_agent_cli.runtime.Sleeper` for the
    reason the capability wiring test gives: the engine's only yield is a sleep,
    so a mod that answers there answers exactly as often as it is asked, with no
    thread and no wall-clock time.

    The stats it reports are fields rather than constants because every case in
    this file is a different world — hungry, calm, bored, threatened — and the
    loop is meant to reach a different decision in each.
    """

    world: CliWorld
    layout: IpcLayout
    session_id: str
    monitor: HeartbeatMonitor
    hunger: float = CALM
    boredom: int = 0
    danger: DangerLevel = DangerLevel.NONE
    mode: SessionMode = SessionMode.AUTONOMOUS
    armed: bool = True
    #: Where the food is. The backpack is the case that needs a transfer first.
    food_container: str = "main"
    stock: tuple[str, ...] = ("food",)
    seq: int = 0
    served: list[str] = field(default_factory=list)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
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
        )

    def inventory(self) -> InventoryView:
        main = main_ref(self.session_id)
        backpack = backpack_ref(self.session_id)
        holder = main if self.food_container == "main" else backpack
        items: list[ItemView] = []
        if "food" in self.stock:
            items.append(tinned_beans(holder))
        if "book" in self.stock:
            items.append(paperback(main))
        return InventoryView(
            containers=[
                make_container(main, ContainerKind.PLAYER_MAIN, name="Inventory"),
                make_container(backpack, ContainerKind.WORN, name="Backpack"),
            ],
            items=items,
        )

    def observe(self) -> Observation:
        self.seq += 1
        observation = make_observation(
            session_id=self.session_id,
            seq=self.seq,
            timestamp_ms=self.world.clock.now_ms,
            player=make_player(
                stats={"hunger": self.hunger, "thirst": CALM, "fatigue": CALM, "endurance": 0.9},
                moodles={"Bored": self.boredom},
            ),
            safety=make_safety(armed=self.armed, mode=self.mode, danger_level=self.danger),
            inventory=self.inventory(),
            nearby=NearbyView(),
        )
        self._append(self.layout.observation_events, observation.to_dict())
        return observation

    def serve(self) -> None:
        """Run whatever the sidecar queued, then ack it."""
        for record in self._commands.read().records:
            command = Command.from_dict(record.payload)
            if command.action is ActionName.CONSUME_EAT:
                self.hunger = FED
            elif command.action is ActionName.INVENTORY_ENSURE_MAIN:
                self.food_container = "main"
            else:
                continue
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


def witness_for_the_observed_save(save_id: str) -> BackupEvidence | None:
    """A backup this test can attribute to whatever save the mod reports.

    The production witness answers None for every save, because a local backup
    is named by its save directory and the observation carries a digest of a key
    that never crosses the boundary — see
    :data:`~pz_agent_cli.autonomy.BACKUP_NOT_ATTRIBUTABLE`, which is asserted on
    below. Standing in for it here is what lets the rest of the autonomous path
    be driven end to end; everything else in these tests is the real assembly.
    """
    return BackupEvidence(save_id=save_id, created_at_ms=1_700_000_000_000, verified=True)


def prove_capability(loop: SidecarLoop, *capabilities: str) -> None:
    """Promote each capability to ``verified`` the way a live run does.

    §7.7 will not let the agent try an ``available_unverified`` capability
    unattended, and a static scan can never produce anything better. The ledger's
    own confirmation path is what a real run reaches through
    ``SidecarLoop._confirm``; this hands it the same engine-shaped result, for
    the action and the evidence keys the probe itself declares, rather than
    editing the report or relaxing the ladder.
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

    def __enter__(self) -> Assembled:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.loop.session is not None:
            self.loop.shutdown(reason="the test finished")


def assemble(
    world: CliWorld,
    *,
    arm: bool = True,
    attributable_backup: bool = True,
    **mod_state: object,
) -> Assembled:
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
        armed=arm,
        mode=SessionMode.AUTONOMOUS if arm else SessionMode.OBSERVE,
        **mod_state,  # type: ignore[arg-type]
    )
    loop.sleep = mod.pump

    attach = loop.attach()
    assert attach.attached, attach.detail
    session = loop.session
    assert session is not None
    mod.session_id = session.session_id
    mod.beat()
    if attributable_backup and isinstance(loop.planner, AutonomyPlanner):
        loop.planner.backup = witness_for_the_observed_save
    if arm:
        armed = loop.arm(SessionMode.AUTONOMOUS)
        assert armed.armed, armed.detail
    mod.observe()
    return Assembled(world=world, workspace=workspace, loop=loop, mod=mod)


# ---------------------------------------------------------------------------
# the planner reaches the loop
# ---------------------------------------------------------------------------


def test_build_loop_wires_a_planner_rather_than_leaving_the_loop_mute(tmp_path: Path) -> None:
    """The assertion that would have caught the original defect.

    Everything below describes what the planner decides; this is the line that
    fails when nobody passes one in, which is the state the branch shipped in.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    assert loop.planner is not None, (
        "build_loop returned a sidecar with no planner: it will observe, guard and "
        "propose nothing, in every mode, however it is armed"
    )
    assert isinstance(loop.planner, AutonomyPlanner)
    assert isinstance(loop.planner.provider, NullProvider)


def test_the_planner_record_says_what_the_assembled_loop_is_really_on(tmp_path: Path) -> None:
    """Written from the loop, so ``status`` cannot disagree with what is running."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    app.build_loop(world.ctx, workspace, limits=LIMITS)

    record = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    assert record is not None
    assert record.wired is True
    assert record.configured == "none"
    assert record.active == "none"
    assert record.fallback_reason == ""


# ---------------------------------------------------------------------------
# what it decides, driven through the assembled loop
# ---------------------------------------------------------------------------


def test_a_hungry_character_with_safe_food_is_fed_by_the_assembled_loop(tmp_path: Path) -> None:
    """One tick, end to end: need derived, item chosen by policy, action verified."""
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)

        outcome = assembled.loop.tick()

        assert len(outcome.results) == 1, assembled.planner.last_detail
        result = outcome.results[0]
        assert result.action == ActionName.CONSUME_EAT.value
        assert result.status is ActionStatus.SUCCEEDED, result.message
        assert assembled.mod.served == [ActionName.CONSUME_EAT.value]
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.outcome is AutonomyOutcome.ACT
        # The item came back from the selection policy, not from the bridge.
        assert decision.item_ref == item_in(main_ref(assembled.mod.session_id), "beans-1")


def test_food_in_the_backpack_is_fetched_first_and_only_that(tmp_path: Path) -> None:
    """One step per tick: the loop re-observes between them, so the bridge does not.

    The plan the provider builds for a tin in a rucksack is two steps. Only the
    first is proposed — the second is composed next tick, against the observation
    that shows where the tin actually ended up, because ``inventory.ensure_main``
    re-mints the reference of the item it moved.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY, food_container="backpack") as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE, INVENTORY_TRANSFER)

        outcome = assembled.loop.tick()

        assert [result.action for result in outcome.results] == [
            ActionName.INVENTORY_ENSURE_MAIN.value
        ], assembled.planner.last_detail
        assert assembled.mod.served == [ActionName.INVENTORY_ENSURE_MAIN.value]


def test_a_character_with_nothing_to_serve_is_left_alone(tmp_path: Path) -> None:
    """An agent that acts because it can rather than because it should is the failure."""
    with assemble(make_world(tmp_path), hunger=CALM) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)

        outcome = assembled.loop.tick()

        assert outcome.results == ()
        assert assembled.mod.served == []
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.outcome is AutonomyOutcome.HOLD
        assert "nothing needs attention" in decision.detail


def test_an_unarmed_loop_proposes_nothing_however_hungry(tmp_path: Path) -> None:
    """The arming gate outranks the planner, and is reached before it."""
    with assemble(make_world(tmp_path), arm=False, hunger=HUNGRY) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)

        outcome = assembled.loop.tick()

        assert outcome.results == ()
        assert assembled.mod.served == []
        assert assembled.planner.last_decision is None, (
            "the planner was consulted on an unarmed loop; _act must return before it"
        )


def test_a_threat_the_guard_is_not_acting_on_still_stops_a_long_action(tmp_path: Path) -> None:
    """``MEDIUM`` danger with nothing running produces no reflex event at all.

    The guard blocks new work at ``HIGH`` and interrupts a vulnerable action at
    ``MEDIUM``; with neither true the loop reaches the planner as usual. Reading
    a book is the long action this build can plan, and the autonomy gate's own
    danger ceiling is what refuses to start one — not a second copy of the guard
    living in the bridge.
    """
    with assemble(
        make_world(tmp_path),
        boredom=BORED,
        stock=("food", "book"),
        danger=DangerLevel.MEDIUM,
    ) as assembled:
        prove_capability(assembled.loop, READ_LITERATURE)

        outcome = assembled.loop.tick()

        assert outcome.events == (), "the guard acted, so this proves nothing about the bridge"
        assert outcome.results == ()
        assert assembled.mod.served == []
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.reason_code is ReasonCode.THREAT_INTERRUPTED


def test_the_same_need_reads_the_book_when_nothing_is_threatening(tmp_path: Path) -> None:
    """The other half of the threat case, so it is the danger that made the difference."""
    with assemble(make_world(tmp_path), boredom=BORED, stock=("food", "book")) as assembled:
        prove_capability(assembled.loop, READ_LITERATURE)

        assembled.loop.tick()

        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.outcome is AutonomyOutcome.ACT
        assert decision.item_ref == item_in(main_ref(assembled.mod.session_id), "book-1")


def test_a_capability_only_a_scan_has_seen_is_not_tried_unattended(tmp_path: Path) -> None:
    """§7.7: ``available_unverified`` is on the ask-the-user side of the line.

    The whole path is wired and the need is real; what is missing is a live run
    proving ``eat_percentage`` against this build. The refusal is the ladder
    working, and it is here so that a later change which quietly grants autonomy
    an unproven capability fails a test rather than an install.
    """
    with assemble(make_world(tmp_path), hunger=HUNGRY) as assembled:
        ledger = assembled.loop.capabilities
        assert ledger is not None and ledger.report is not None
        assert ledger.report.state(EAT_PERCENTAGE) is CapabilityState.AVAILABLE_UNVERIFIED

        outcome = assembled.loop.tick()

        assert outcome.results == ()
        assert assembled.mod.served == []
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert decision.outcome is AutonomyOutcome.ASK_USER


# ---------------------------------------------------------------------------
# the backup this build cannot attribute
# ---------------------------------------------------------------------------


def test_without_an_attributable_backup_the_agent_asks_instead_of_acting(tmp_path: Path) -> None:
    """The production wiring, unmodified: §7.7's unbacked-save rule, reached honestly."""
    with assemble(make_world(tmp_path), hunger=HUNGRY, attributable_backup=False) as assembled:
        prove_capability(assembled.loop, EAT_PERCENTAGE)

        outcome = assembled.loop.tick()

        assert outcome.results == ()
        decision = assembled.planner.last_decision
        assert decision is not None
        assert decision.outcome is AutonomyOutcome.ASK_USER
        assert decision.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "no backup" in decision.detail


def test_status_carries_the_reason_autonomy_will_ask_rather_than_act(tmp_path: Path) -> None:
    """A refusal nobody can see is the defect this file exists to stop repeating."""
    world = make_world(tmp_path)
    app.build_loop(world.ctx, resolve_workspace(world.ctx), limits=LIMITS)

    world.reset_streams()
    assert world.run("status") == 0

    assert BACKUP_NOT_ATTRIBUTABLE in world.stdout


# ---------------------------------------------------------------------------
# a provider that cannot be constructed
# ---------------------------------------------------------------------------


def write_config(workspace: Workspace, body: str) -> None:
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(body, encoding="utf-8")


MODEL_CONFIG: Final = """
[planner]
provider = "openai_compatible"

[planner.openai_compatible]
base_url = "http://127.0.0.1:8080"
model = "qwen2.5-7b-instruct"
api_key_env = "PZ_AGENT_TEST_KEY"
"""


def test_a_configured_provider_with_no_key_falls_back_to_the_deterministic_path(
    tmp_path: Path,
) -> None:
    """Not a failure to start, and not a model quietly answered for by policy."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    write_config(workspace, MODEL_CONFIG)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    planner = loop.planner
    assert isinstance(planner, AutonomyPlanner)
    assert isinstance(planner.provider, NullProvider)
    record = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    assert record is not None
    assert record.configured == "openai_compatible"
    assert record.active == "none"
    assert "PZ_AGENT_TEST_KEY" in record.fallback_reason


def test_the_fallback_is_visible_in_status_rather_than_only_in_the_silence(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    write_config(workspace, MODEL_CONFIG)
    app.build_loop(world.ctx, workspace, limits=LIMITS)

    world.reset_streams()
    assert world.run("status") == 0

    printed = world.stdout
    assert "FELL BACK from openai_compatible" in printed
    assert "PZ_AGENT_TEST_KEY" in printed


def test_a_configured_provider_whose_key_is_set_is_the_one_that_answers(
    tmp_path: Path,
) -> None:
    """The fallback is a fallback, not a refusal to use a model at all."""
    world = make_world(tmp_path)
    world.ctx.env["PZ_AGENT_TEST_KEY"] = "not-a-real-key"  # type: ignore[index]
    workspace = resolve_workspace(world.ctx)
    write_config(workspace, MODEL_CONFIG)

    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)

    planner = loop.planner
    assert isinstance(planner, AutonomyPlanner)
    assert planner.provider.name == "openai_compatible"
    record = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    assert record is not None
    assert record.active == "openai_compatible"
    assert record.fallback_reason == ""


def test_a_state_directory_no_sidecar_has_assembled_in_says_so(tmp_path: Path) -> None:
    """Absent is not the same answer as unwired, and must not read like one."""
    world = make_world(tmp_path)

    assert world.run("status") == 0

    assert "no sidecar has assembled one in this state directory" in world.stdout
