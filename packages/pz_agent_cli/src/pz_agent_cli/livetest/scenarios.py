"""The twenty live scenarios, as data.

``docs/LIVE_TEST_PLAYBOOK.md`` is generated from this module, so every field an
operator needs on a Windows machine with the game open lives here rather than in
prose: what to build in the world first, what state the character must be in,
the exact command, what the human does in-game, what should happen, and — the
part that decides the verdict — the postconditions.

**Postconditions are checked, not asserted.** The smoke harness
(:mod:`pz_agent_cli.smoke`) keeps its assertions as prose because it never
evaluates them; a reviewer does. Here the runner decides PASS, so prose would
mean the verdict came from whoever wrote the observations. Each postcondition
therefore carries a :class:`Check` from a small closed vocabulary that
:mod:`pz_agent_cli.livetest.runner` evaluates deterministically, plus a
``statement`` that says the same thing to a human. If the two ever disagree, the
check is what ran.

A postcondition can only pass on a value that was *observed*. There is no check
that succeeds on an absent field, and none that a driver can satisfy by
reporting that it tried.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pz_agent_core.protocol import JsonDict


class Check(StrEnum):
    """How a postcondition is decided.

    Deliberately small. Every member is total over the two inputs the runner
    has — the observations mapping and the before/after snapshot pair — and
    every one of them fails when the field it names was never observed.
    """

    #: Present in the observations and not empty. The weakest check there is,
    #: and still stronger than "the call returned".
    OBSERVED = "observed"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    EQUALS = "equals"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    #: Read from the before/after snapshots by the same dotted path.
    INCREASED = "increased"
    DECREASED = "decreased"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

    @property
    def reads_snapshots(self) -> bool:
        """True when the check compares the before and after snapshots."""
        return self in {Check.INCREASED, Check.DECREASED, Check.CHANGED, Check.UNCHANGED}


@dataclass(frozen=True, slots=True)
class Postcondition:
    """One thing that must be observed for a scenario to pass.

    ``key`` is stable and appears in ``result.json``; renaming one invalidates
    the evidence that used the old name, which is the intent.
    """

    key: str
    statement: str
    check: Check
    field: str
    expected: str | int | float | bool | None = None
    why: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "key": self.key,
            "statement": self.statement,
            "check": self.check.value,
            "field": self.field,
            "expected": self.expected,
            "reads_snapshots": self.check.reads_snapshots,
            "why": self.why,
        }


@dataclass(frozen=True, slots=True)
class LiveScenario:
    """Everything an operator and the runner need for one scenario."""

    id: str
    title: str
    goal: str
    world_preparation: tuple[str, ...]
    required_state: tuple[str, ...]
    command: str
    operator_steps: tuple[str, ...]
    expected_result: str
    postconditions: tuple[Postcondition, ...]
    time_budget_s: int
    logs: tuple[str, ...]
    suspect_module: str
    #: Scenarios that repeat one operation and report a latency distribution.
    #: Only these carry p50/p95; the rest record ``null`` rather than a number
    #: derived from a single sample.
    measures_latency: bool = False
    screenshots_required: bool = False

    def postcondition(self, key: str) -> Postcondition | None:
        for condition in self.postconditions:
            if condition.key == key:
                return condition
        return None

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "world_preparation": list(self.world_preparation),
            "required_state": list(self.required_state),
            "command": self.command,
            "operator_steps": list(self.operator_steps),
            "expected_result": self.expected_result,
            "postconditions": [condition.to_dict() for condition in self.postconditions],
            "time_budget_s": self.time_budget_s,
            "logs": list(self.logs),
            "suspect_module": self.suspect_module,
            "measures_latency": self.measures_latency,
            "screenshots_required": self.screenshots_required,
        }


# Log filenames, spelled once. The exchange-directory names mirror
# ``pz_agent_core.ipc.layout``; ``console.txt`` is the game's own and is the
# only place a mod-side Lua error appears.
_CONSOLE: Final = "console.txt"
_SIDECAR_LOG: Final = "pz-agent.log"
_SIDECAR_JSONL: Final = "pz-agent.jsonl"
_ACKS: Final = "command.ack.0001.jsonl"
_EVENTS: Final = "observation.events.0001.jsonl"
_QUEUE: Final = "command.queue.0001.jsonl"
_HEARTBEAT: Final = "heartbeat.game.json"

_BASE_LOGS: Final = (_CONSOLE, _SIDECAR_LOG, _ACKS)


SCENARIOS: Final[tuple[LiveScenario, ...]] = (
    LiveScenario(
        id="S01_INSTALL",
        title="Mod installs, loads and is visible from both sides",
        goal=(
            "Prove the bridge mod is executing inside Build 42.20, not merely present on "
            "disk — the failure this catches is a mod that installed cleanly and threw "
            "during load, which looks identical to an idle exchange directory."
        ),
        world_preparation=(
            "Create a dedicated test world; do not open the main save.",
            "Take a backup with 'pz-agent backup-save' before the first launch.",
        ),
        required_state=(
            "Project Zomboid closed.",
            "No sidecar running.",
        ),
        command="pz-agent install-mod && pz-agent doctor --json",
        operator_steps=(
            "Run install-mod, then launch the game.",
            "Enable 'PZ Agent Bridge' in the Mods screen and load the test save.",
            "Leave the game on the loaded save and run doctor again.",
        ),
        expected_result=(
            "doctor resolves the install, the Zomboid directory and the exchange "
            "directory, and the game heartbeat file exists with an advancing timestamp."
        ),
        postconditions=(
            Postcondition(
                key="mod_files_on_disk",
                statement="mod.info exists in the Zomboid mods directory",
                check=Check.IS_TRUE,
                field="install.mod_info_present",
            ),
            Postcondition(
                key="heartbeat_written",
                statement="heartbeat.game.json exists in the exchange directory",
                check=Check.IS_TRUE,
                field="ipc.game_heartbeat_present",
                why=(
                    "Present on disk and enabled in the menu both hold for a mod that "
                    "crashed during load. Only a written heartbeat proves it is running."
                ),
            ),
            Postcondition(
                key="heartbeat_advances",
                statement="the heartbeat timestamp is newer in the second reading",
                check=Check.INCREASED,
                field="ipc.game_heartbeat_ms",
            ),
            Postcondition(
                key="build_string",
                statement="the detected game build is recorded",
                check=Check.OBSERVED,
                field="game.build",
            ),
        ),
        time_budget_s=600,
        logs=(_CONSOLE, _SIDECAR_LOG, _HEARTBEAT),
        suspect_module="pz-mod/42/media/lua/client/PZAgent/ (load order), pz_agent_cli.modinstall",
        screenshots_required=True,
    ),
    LiveScenario(
        id="S02_HEARTBEAT",
        title="Session handshake and two-way heartbeat",
        goal=(
            "Prove the sidecar and the mod agree on one session id and each sees the "
            "other's liveness, which every later scenario assumes."
        ),
        world_preparation=("Test save loaded and standing still indoors.",),
        required_state=(
            "Game running with the save loaded.",
            "Mod heartbeat already advancing (S01 passed).",
        ),
        command="pz-agent start && pz-agent status --json",
        operator_steps=(
            "Start the sidecar and leave the character idle for one minute.",
            "Run status twice, roughly thirty seconds apart.",
        ),
        expected_result=(
            "status reports the mod present, one session id shared by both sides, mode "
            "OBSERVE, and both heartbeats advancing."
        ),
        postconditions=(
            Postcondition(
                key="session_id_agreed",
                statement="the sidecar and the snapshot name the same session id",
                check=Check.IS_TRUE,
                field="session.ids_agree",
                why=(
                    "Two ids means the two sides resolved different exchange "
                    "directories, and every subsequent command would be written where "
                    "nothing reads it."
                ),
            ),
            Postcondition(
                key="mode_is_observe",
                statement="the session attaches in OBSERVE",
                check=Check.EQUALS,
                field="session.mode",
                expected="OBSERVE",
            ),
            Postcondition(
                key="sidecar_heartbeat_advances",
                statement="heartbeat.sidecar.json is newer in the second reading",
                check=Check.INCREASED,
                field="ipc.sidecar_heartbeat_ms",
            ),
            Postcondition(
                key="game_heartbeat_advances",
                statement="heartbeat.game.json is newer in the second reading",
                check=Check.INCREASED,
                field="ipc.game_heartbeat_ms",
            ),
        ),
        time_budget_s=300,
        logs=(_CONSOLE, _SIDECAR_LOG, _HEARTBEAT, _EVENTS),
        suspect_module="pz_agent_core.session, PZAgent/Session.lua",
    ),
    LiveScenario(
        id="S03_ARM_DISARM",
        title="Arming grants authority and disarming takes it back",
        goal=(
            "Prove the mode transition is applied by the mod and not only claimed by the "
            "CLI: the snapshot's safety.mode is the value the game is acting on."
        ),
        world_preparation=("Character idle and safe, no zombies in view.",),
        required_state=("Sidecar attached in OBSERVE (S02 passed).",),
        command="pz-agent arm --mode assisted && pz-agent status --json && pz-agent disarm",
        operator_steps=("Arm in assisted mode, read status, then disarm and read status again.",),
        expected_result=(
            "safety.mode in the snapshot goes OBSERVE -> ASSISTED -> OBSERVE, and the "
            "CLI agrees with the snapshot at every step."
        ),
        postconditions=(
            Postcondition(
                key="armed_in_snapshot",
                statement="the snapshot reports ASSISTED after arm",
                check=Check.EQUALS,
                field="arm.snapshot_mode",
                expected="ASSISTED",
                why="The CLI publishes a request; only the snapshot proves the mod applied it.",
            ),
            Postcondition(
                key="cli_agrees_when_armed",
                statement="the CLI and the snapshot report the same mode",
                check=Check.IS_TRUE,
                field="arm.cli_agrees",
            ),
            Postcondition(
                key="disarmed_in_snapshot",
                statement="the snapshot reports OBSERVE after disarm",
                check=Check.EQUALS,
                field="disarm.snapshot_mode",
                expected="OBSERVE",
            ),
        ),
        time_budget_s=300,
        logs=(_CONSOLE, _SIDECAR_LOG, _QUEUE, _ACKS),
        suspect_module="pz_agent_cli.supervisor, PZAgent/Safety.lua",
    ),
    LiveScenario(
        id="S04_MOVE",
        title="Move to a square and observe arrival",
        goal=(
            "Prove a movement command reaches the character and that success is decided "
            "by the position that was read back, not by the command being accepted."
        ),
        world_preparation=(
            "Stand in an open indoor room with a clear three-tile walk.",
            "Note the starting tile coordinates from 'pz-agent status --json'.",
        ),
        required_state=("Session armed in ASSISTED.",),
        command="pz-agent live-test run --scenario S04_MOVE --observations <file>",
        operator_steps=(
            "Issue a move to a square three tiles away.",
            "Do not touch the keyboard while the character walks.",
        ),
        expected_result=(
            "The character arrives; the action result is succeeded with the arrival "
            "position as its evidence."
        ),
        postconditions=(
            Postcondition(
                key="position_changed",
                statement="the player's tile differs between before and after",
                check=Check.CHANGED,
                field="player.position",
            ),
            Postcondition(
                key="arrived_at_target",
                statement="the final tile equals the requested square",
                check=Check.IS_TRUE,
                field="move.arrived_at_target",
                why=(
                    "Position changed is not arrival. A character shoved one tile by a "
                    "zombie also changed position."
                ),
            ),
            Postcondition(
                key="reason_code",
                statement="the action result carries POSTCONDITION_MET",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="POSTCONDITION_MET",
            ),
        ),
        time_budget_s=300,
        logs=(*_BASE_LOGS, _QUEUE, _EVENTS),
        suspect_module="actions/adapters/movement.py, PZAgent/adapters/Movement.lua",
        measures_latency=True,
    ),
    LiveScenario(
        id="S05_BLOCKED_PATH",
        title="An unreachable square fails with a named reason",
        goal=(
            "Prove a movement that cannot complete reports a specific failure rather "
            "than timing out into a generic error or, worse, reporting success."
        ),
        world_preparation=(
            "Find a square behind a locked door or across a wall.",
            "Note its coordinates.",
        ),
        required_state=("Session armed in ASSISTED.", "Character not in combat."),
        command="pz-agent live-test run --scenario S05_BLOCKED_PATH --observations <file>",
        operator_steps=(
            "Issue a move to the unreachable square.",
            "Watch until the action ends on its own; do not cancel it.",
        ),
        expected_result=(
            "The action fails with PATH_NOT_FOUND or PATH_STUCK and the character is unhurt."
        ),
        postconditions=(
            Postcondition(
                key="failed_not_succeeded",
                statement="the action status is failed",
                check=Check.EQUALS,
                field="action_result.status",
                expected="failed",
            ),
            Postcondition(
                key="path_reason_code",
                statement="the reason code names the pathing failure",
                check=Check.OBSERVED,
                field="action_result.reason_code",
                why=(
                    "A generic INTERNAL_ERROR here would mean the adapter cannot tell a "
                    "blocked path from a crash, and the operator has nothing to act on."
                ),
            ),
            Postcondition(
                key="ended_within_budget",
                statement="the action ended within its timeout",
                check=Check.AT_MOST,
                field="action_result.duration_ms",
                expected=120_000,
            ),
            Postcondition(
                key="health_unchanged",
                statement="the character took no damage",
                check=Check.UNCHANGED,
                field="player.health",
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _QUEUE),
        suspect_module="actions/adapters/movement.py, ActionRuntime.lua (timeout path)",
    ),
    LiveScenario(
        id="S06_MANUAL_TAKEOVER",
        title="Moving by hand cancels the agent's action",
        goal=(
            "Prove the user always wins: a keypress during an agent action ends that "
            "action and returns the session to OBSERVE."
        ),
        world_preparation=("A long clear walk, at least ten tiles.",),
        required_state=("Session armed in ASSISTED.",),
        command="pz-agent live-test run --scenario S06_MANUAL_TAKEOVER --observations <file>",
        operator_steps=(
            "Issue a move to a square ten tiles away.",
            "After roughly two seconds, press a movement key yourself.",
            "Keep moving manually for a few seconds, then stop.",
        ),
        expected_result=(
            "The in-flight action ends as USER_TAKEOVER, the session drops to OBSERVE, "
            "and nothing the player queued was cancelled."
        ),
        postconditions=(
            Postcondition(
                key="takeover_reason",
                statement="the action result reason code is USER_TAKEOVER",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="USER_TAKEOVER",
            ),
            Postcondition(
                key="disarmed_after_takeover",
                statement="the snapshot reports OBSERVE afterwards",
                check=Check.EQUALS,
                field="safety.mode_after",
                expected="OBSERVE",
            ),
            Postcondition(
                key="player_queue_untouched",
                statement="no action the player queued was cancelled",
                check=Check.IS_TRUE,
                field="safety.player_queue_intact",
                why=(
                    "Cancelling the player's own queued work is the agent overriding "
                    "them at the exact moment they took control."
                ),
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="PZAgent/Safety.lua (manual takeover), Ownership.lua",
    ),
    LiveScenario(
        id="S07_NESTED_INVENTORY",
        title="Transfer an item out of a bag inside a bag",
        goal=(
            "Prove references survive nesting: the item is identified inside a container "
            "that is itself inside a container, and it lands in the main inventory."
        ),
        world_preparation=(
            "Put a backpack inside another backpack.",
            "Put one uniquely named item in the inner backpack.",
            "Wear the outer backpack.",
        ),
        required_state=("Session armed in ASSISTED.", "Main inventory has free capacity."),
        command="pz-agent live-test run --scenario S07_NESTED_INVENTORY --observations <file>",
        operator_steps=(
            "Issue a transfer of the named item to the main inventory.",
            "Do not open or close any container by hand while it runs.",
        ),
        expected_result=(
            "The item is in the main inventory afterwards and gone from the inner bag."
        ),
        postconditions=(
            Postcondition(
                key="item_in_main",
                statement="the item is present in the main inventory after the action",
                check=Check.IS_TRUE,
                field="transfer.item_in_main_after",
            ),
            Postcondition(
                key="item_left_source",
                statement="the item is absent from the inner container after the action",
                check=Check.IS_FALSE,
                field="transfer.item_in_source_after",
                why=(
                    "Present in both would mean the reference resolved to a different "
                    "object of the same type, which is exactly the nesting bug."
                ),
            ),
            Postcondition(
                key="main_count_increased",
                statement="the main inventory item count went up",
                check=Check.INCREASED,
                field="inventory.main_count",
            ),
            Postcondition(
                key="reason_code",
                statement="the action result carries POSTCONDITION_MET",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="POSTCONDITION_MET",
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _QUEUE),
        suspect_module="actions/adapters/inventory.py, protocol/refs.py, PZAgent/Refs.lua",
    ),
    LiveScenario(
        id="S08_UNSAFE_FOOD",
        title="Rotten food is refused and safe food is chosen",
        goal=(
            "Prove the deterministic food policy runs against real item data: the rotten "
            "item is never selected, and the hunger drop is observed rather than assumed."
        ),
        world_preparation=(
            "Place one rotten food item and one fresh food item in the main inventory.",
            "Let the character get hungry (skip a meal or two).",
        ),
        required_state=("Session armed in ASSISTED.", "Hunger clearly above zero."),
        command="pz-agent live-test run --scenario S08_UNSAFE_FOOD --observations <file>",
        operator_steps=(
            "Ask the agent to eat.",
            "Watch which item it picks in the inventory panel.",
        ),
        expected_result="The fresh item is eaten, the rotten one is untouched, and hunger falls.",
        postconditions=(
            Postcondition(
                key="chosen_is_safe",
                statement="the chosen item is the fresh one",
                check=Check.IS_TRUE,
                field="selection.chosen_is_safe",
            ),
            Postcondition(
                key="rotten_untouched",
                statement="the rotten item is still in the inventory afterwards",
                check=Check.IS_TRUE,
                field="selection.rotten_still_present",
            ),
            Postcondition(
                key="hunger_fell",
                statement="hunger is lower after than before",
                check=Check.DECREASED,
                field="player.hunger",
                why="Eating that does not change hunger is indistinguishable from not eating.",
            ),
            Postcondition(
                key="rationale_recorded",
                statement="the selection rationale is recorded",
                check=Check.OBSERVED,
                field="selection.rationale",
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="policy/food.py, actions/adapters/consume.py",
    ),
    LiveScenario(
        id="S09_DRINK",
        title="Drink from a safe source and observe thirst fall",
        goal=(
            "Prove drink selection prefers a safe source over tainted water and that "
            "thirst is read back, not inferred from the action starting."
        ),
        world_preparation=(
            "Provide a water bottle and, if available, a tainted source nearby.",
            "Let the character get thirsty.",
        ),
        required_state=("Session armed in ASSISTED.", "Thirst clearly above zero."),
        command="pz-agent live-test run --scenario S09_DRINK --observations <file>",
        operator_steps=("Ask the agent to drink.", "Note the source it used."),
        expected_result="The safe source is used and thirst falls.",
        postconditions=(
            Postcondition(
                key="thirst_fell",
                statement="thirst is lower after than before",
                check=Check.DECREASED,
                field="player.thirst",
            ),
            Postcondition(
                key="source_is_safe",
                statement="the chosen source was not tainted",
                check=Check.IS_TRUE,
                field="selection.source_is_safe",
            ),
            Postcondition(
                key="chosen_ref",
                statement="the chosen source reference is recorded",
                check=Check.OBSERVED,
                field="selection.chosen_ref",
            ),
        ),
        time_budget_s=360,
        logs=(*_BASE_LOGS,),
        suspect_module="policy/selection.py (drink), actions/adapters/consume.py",
    ),
    LiveScenario(
        id="S10_READ",
        title="Read literature with observed progress",
        goal=(
            "Prove reading starts and advances. A read that starts and never progresses "
            "is indistinguishable from one that failed silently."
        ),
        world_preparation=(
            "Put a skill book the character can actually use in the inventory.",
            "Sit somewhere safe and lit.",
        ),
        required_state=(
            "Session armed in ASSISTED.",
            "Character is not Illiterate and meets the book's skill requirement.",
        ),
        command="pz-agent live-test run --scenario S10_READ --observations <file>",
        operator_steps=(
            "Ask the agent to read toward a stated skill goal.",
            "Let it read for at least one minute of game time.",
        ),
        expected_result=(
            "Reading starts, pages read advances, and the book matches the stated goal."
        ),
        postconditions=(
            Postcondition(
                key="reading_started",
                statement="the reading action reported started",
                check=Check.IS_TRUE,
                field="action_result.evidence.reading_started",
            ),
            Postcondition(
                key="progress_advanced",
                statement="pages read is higher after than before",
                check=Check.INCREASED,
                field="literature.pages_read",
                why="Progress is the postcondition; starting is not.",
            ),
            Postcondition(
                key="book_matches_goal",
                statement="the chosen book matches the stated skill goal",
                check=Check.IS_TRUE,
                field="selection.matches_goal",
            ),
        ),
        time_budget_s=600,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="policy/literature.py, actions/adapters/literature.py",
    ),
    LiveScenario(
        id="S11_CONTAINER",
        title="Open a world container and read its contents",
        goal=(
            "Prove a world container reference resolves to the container the operator "
            "opened, and that its listed contents match what the game shows."
        ),
        world_preparation=(
            "Find a crate or counter with at least three distinguishable items.",
            "Stand within reach of it.",
        ),
        required_state=("Session armed in ASSISTED.",),
        command="pz-agent live-test run --scenario S11_CONTAINER --observations <file>",
        operator_steps=(
            "Ask the agent to inspect the nearby container.",
            "Compare the reported contents against the in-game panel, item by item.",
        ),
        expected_result="The reported contents match the panel exactly.",
        postconditions=(
            Postcondition(
                key="container_ref",
                statement="the resolved container reference is recorded",
                check=Check.OBSERVED,
                field="container.ref",
                why=(
                    "A world container reference carries colons in its tail; a naive "
                    "split yields a valid reference to a different object."
                ),
            ),
            Postcondition(
                key="contents_match",
                statement="the reported contents match the in-game panel",
                check=Check.IS_TRUE,
                field="container.contents_match_panel",
            ),
            Postcondition(
                key="item_count",
                statement="at least three items were listed",
                check=Check.AT_LEAST,
                field="container.item_count",
                expected=3,
            ),
        ),
        time_budget_s=360,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="actions/adapters/container.py, protocol/refs.py, PZAgent/Refs.lua",
        screenshots_required=True,
    ),
    LiveScenario(
        id="S12_EQUIPMENT",
        title="Equip an item into the correct slot",
        goal="Prove an equip is verified by reading the slot back, not by the call returning.",
        world_preparation=("Put one weapon and one bag in the main inventory, hands empty.",),
        required_state=("Session armed in ASSISTED.", "Both hands free."),
        command="pz-agent live-test run --scenario S12_EQUIPMENT --observations <file>",
        operator_steps=("Ask the agent to equip the weapon in the primary hand.",),
        expected_result=(
            "The weapon is in the primary hand afterwards, and the slot reads back with "
            "its reference."
        ),
        postconditions=(
            Postcondition(
                key="slot_occupied",
                statement="the primary hand holds the requested item afterwards",
                check=Check.IS_TRUE,
                field="equipment.primary_matches_request",
            ),
            Postcondition(
                key="hands_changed",
                statement="the hands view differs between before and after",
                check=Check.CHANGED,
                field="player.hands",
            ),
            Postcondition(
                key="reason_code",
                statement="the action result carries POSTCONDITION_MET",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="POSTCONDITION_MET",
            ),
        ),
        time_budget_s=300,
        logs=(*_BASE_LOGS,),
        suspect_module="actions/adapters/equipment.py",
    ),
    LiveScenario(
        id="S13_MEDICAL",
        title="Treat a wound and observe the wound state change",
        goal=(
            "Prove medical treatment is decided by the wound list read back afterwards. "
            "This is the scenario where a fabricated success would be most harmful."
        ),
        world_preparation=(
            "Take a minor scratch or laceration deliberately (a fall, not a zombie).",
            "Carry bandages.",
        ),
        required_state=("Session armed in ASSISTED.", "Exactly one untreated wound."),
        command="pz-agent live-test run --scenario S13_MEDICAL --observations <file>",
        operator_steps=(
            "Ask the agent to treat the wound.",
            "Open the health panel and confirm what changed.",
        ),
        expected_result=(
            "The wound is bandaged, and the health panel agrees with the reported state."
        ),
        postconditions=(
            Postcondition(
                key="wound_bandaged",
                statement="the wound reads as bandaged after the action",
                check=Check.IS_TRUE,
                field="medical.wound_bandaged_after",
            ),
            Postcondition(
                key="untreated_count_fell",
                statement="the number of untreated wounds went down",
                check=Check.DECREASED,
                field="player.untreated_wounds",
            ),
            Postcondition(
                key="panel_agrees",
                statement="the in-game health panel agrees with the reported state",
                check=Check.IS_TRUE,
                field="medical.panel_agrees",
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="policy/medical.py, actions/adapters/medical.py",
        screenshots_required=True,
    ),
    LiveScenario(
        id="S14_SLEEP_REST",
        title="Rest reduces fatigue and is interruptible",
        goal=(
            "Prove a long self-directed action progresses, reports fatigue falling, and "
            "can be stopped on request."
        ),
        world_preparation=(
            "Secure a room with a bed; close and, if possible, barricade the door.",
            "Let the character get tired.",
        ),
        required_state=(
            "Session armed in ASSISTED.",
            "Fatigue clearly above zero.",
            "No zombies in view.",
        ),
        command="pz-agent live-test run --scenario S14_SLEEP_REST --observations <file>",
        operator_steps=(
            "Ask the agent to rest.",
            "After a while, ask it to stop, and confirm the character wakes.",
        ),
        expected_result=(
            "Fatigue falls, and the stop request ends the action as CANCELLED_BY_REQUEST."
        ),
        postconditions=(
            Postcondition(
                key="fatigue_fell",
                statement="fatigue is lower after than before",
                check=Check.DECREASED,
                field="player.fatigue",
            ),
            Postcondition(
                key="stop_honoured",
                statement="the stop request ended the action",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="CANCELLED_BY_REQUEST",
            ),
            Postcondition(
                key="awake_after",
                statement="the character is awake afterwards",
                check=Check.IS_TRUE,
                field="player.awake_after",
            ),
        ),
        time_budget_s=900,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="actions/adapters/survival.py, ActionRuntime.lua (poll loop)",
    ),
    LiveScenario(
        id="S15_ZOMBIE_INTERRUPT",
        title="A zombie interrupts a running action",
        goal=(
            "Prove the reflex guard sees a real threat and ends the in-flight action "
            "before it completes."
        ),
        world_preparation=(
            "Lure one zombie into an adjacent room and close the door.",
            "Start with the character safe and the door shut.",
        ),
        required_state=("Session armed in ASSISTED.", "Exactly one zombie nearby, contained."),
        command="pz-agent live-test run --scenario S15_ZOMBIE_INTERRUPT --observations <file>",
        operator_steps=(
            "Start a long action (reading or resting).",
            "Open the door so the zombie reaches the character.",
            "Deal with the zombie yourself once the action has ended.",
        ),
        expected_result="The action ends as THREAT_INTERRUPTED and the session drops to OBSERVE.",
        postconditions=(
            Postcondition(
                key="threat_reason",
                statement="the action ended with THREAT_INTERRUPTED",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="THREAT_INTERRUPTED",
            ),
            Postcondition(
                key="zombie_observed",
                statement="the nearby zombie appears in the snapshot",
                check=Check.AT_LEAST,
                field="threat.zombies_seen",
                expected=1,
                why=(
                    "An interrupt with no zombie in the snapshot means the guard fired "
                    "on something else and the reason code is wrong."
                ),
            ),
            Postcondition(
                key="disarmed",
                statement="the snapshot reports OBSERVE afterwards",
                check=Check.EQUALS,
                field="safety.mode_after",
                expected="OBSERVE",
            ),
        ),
        time_budget_s=600,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="safety/reflex.py, PZAgent/Safety.lua (threat scan)",
    ),
    LiveScenario(
        id="S16_STALE_SIDECAR",
        title="The mod stops acting when the sidecar goes silent",
        goal=(
            "Prove the mod does not keep acting when nothing is supervising it. This is "
            "the safety property that makes an unattended crash survivable."
        ),
        world_preparation=("Character safe indoors, door closed.",),
        required_state=("Session armed in ASSISTED.", "No action in flight."),
        command="pz-agent live-test run --scenario S16_STALE_SIDECAR --observations <file>",
        operator_steps=(
            "Kill the sidecar process — close its window, do not run 'pz-agent stop'.",
            "Watch the game for the heartbeat timeout to elapse.",
            "Read the snapshot afterwards.",
        ),
        expected_result="The mod disarms itself and reports the sidecar heartbeat as stale.",
        postconditions=(
            Postcondition(
                key="mod_disarmed_itself",
                statement="the snapshot reports OBSERVE after the timeout",
                check=Check.EQUALS,
                field="safety.mode_after",
                expected="OBSERVE",
            ),
            Postcondition(
                key="staleness_detected",
                statement="the mod reports the sidecar heartbeat as stale",
                check=Check.IS_TRUE,
                field="session.sidecar_stale",
            ),
            Postcondition(
                key="no_action_after",
                statement="no command was executed after the sidecar went silent",
                check=Check.EQUALS,
                field="session.acks_after_silence",
                expected=0,
            ),
        ),
        time_budget_s=600,
        logs=(_CONSOLE, _ACKS, _HEARTBEAT, _EVENTS),
        suspect_module="PZAgent/Session.lua (heartbeat staleness), Safety.lua",
    ),
    LiveScenario(
        id="S17_RESTART_RECOVERY",
        title="Restarting the sidecar recovers rather than replays",
        goal=(
            "Prove restart recovery reads where it left off: no command is executed "
            "twice, and the new session is cleanly minted."
        ),
        world_preparation=("Character safe indoors.",),
        required_state=("A completed action in the journal from an earlier scenario.",),
        command="pz-agent start && pz-agent status --json",
        operator_steps=(
            "Note the last ack sequence number.",
            "Stop the sidecar and start it again with the game still running.",
            "Read status and the ack journal.",
        ),
        expected_result=(
            "The sidecar reattaches, no earlier command is re-executed, and the mode is OBSERVE."
        ),
        postconditions=(
            Postcondition(
                key="reattached",
                statement="the sidecar reports the mod present after restart",
                check=Check.IS_TRUE,
                field="session.reattached",
            ),
            Postcondition(
                key="no_replay",
                statement="no command id from before the restart was acked again",
                check=Check.EQUALS,
                field="session.replayed_command_count",
                expected=0,
                why=(
                    "A replay after restart would repeat a real in-game action — eating "
                    "the last safe food twice, for instance."
                ),
            ),
            Postcondition(
                key="mode_observe",
                statement="the restarted sidecar attaches in OBSERVE",
                check=Check.EQUALS,
                field="session.mode",
                expected="OBSERVE",
            ),
        ),
        time_budget_s=420,
        logs=(_CONSOLE, _SIDECAR_LOG, _SIDECAR_JSONL, _ACKS, _QUEUE),
        suspect_module="pz_agent_cli.runtime (recovery), ipc/journal.py",
    ),
    LiveScenario(
        id="S18_PANIC",
        title="Panic stop clears only what the mod owns",
        goal=(
            "Prove panic stop cancels the agent's timed actions and leaves the player's "
            "own queued work alone."
        ),
        world_preparation=("A long task the player can queue by hand nearby.",),
        required_state=("Session armed in ASSISTED.",),
        command="pz-agent disarm",
        operator_steps=(
            "Queue a long action yourself by hand.",
            "Have the agent start a long action of its own.",
            "Trigger the panic stop.",
        ),
        expected_result=(
            "The agent's action ends as PANIC_STOP, the session is OBSERVE, and the "
            "player's queued action is still running."
        ),
        postconditions=(
            Postcondition(
                key="panic_reason",
                statement="the agent's action ended with PANIC_STOP",
                check=Check.EQUALS,
                field="action_result.reason_code",
                expected="PANIC_STOP",
            ),
            Postcondition(
                key="player_action_survived",
                statement="the player's queued action is still present afterwards",
                check=Check.IS_TRUE,
                field="safety.player_action_survived",
            ),
            Postcondition(
                key="disarmed",
                statement="the snapshot reports OBSERVE afterwards",
                check=Check.EQUALS,
                field="safety.mode_after",
                expected="OBSERVE",
            ),
        ),
        time_budget_s=420,
        logs=(*_BASE_LOGS, _EVENTS),
        suspect_module="PZAgent/Ownership.lua, Safety.lua (panic latch)",
    ),
    LiveScenario(
        id="S19_AUTONOMOUS_30_MIN",
        title="Thirty minutes autonomous inside the bounds",
        goal=(
            "Prove the autonomous loop stays inside its radius, re-observes between "
            "actions, and never runs two actions at once, for half an hour."
        ),
        world_preparation=(
            "Secure a house with food, water and a bed.",
            "Set the anchor to that house and the radius to its block.",
            "Take a fresh backup immediately before starting.",
        ),
        required_state=("Session armed in AUTONOMOUS.", "No zombies inside the perimeter."),
        command="pz-agent live-test run --scenario S19_AUTONOMOUS_30_MIN --observations <file>",
        operator_steps=(
            "Arm in autonomous and leave the character alone for thirty minutes.",
            "Watch; intervene only if the save is at risk.",
        ),
        expected_result=(
            "The character survives, stays inside the radius, and every action is "
            "followed by a re-observation."
        ),
        postconditions=(
            Postcondition(
                key="ran_full_duration",
                statement="the run lasted at least thirty minutes",
                check=Check.AT_LEAST,
                field="endurance.duration_s",
                expected=1800,
            ),
            Postcondition(
                key="stayed_in_radius",
                statement="the character never left the configured radius",
                check=Check.IS_TRUE,
                field="autonomy.stayed_in_radius",
            ),
            Postcondition(
                key="single_action_at_a_time",
                statement="never more than one agent action was in flight",
                check=Check.AT_MOST,
                field="autonomy.max_concurrent_actions",
                expected=1,
            ),
            Postcondition(
                key="survived",
                statement="the character is alive at the end",
                check=Check.IS_TRUE,
                field="player.alive_after",
            ),
            Postcondition(
                key="no_unbounded_growth",
                statement="the journals stayed within their rotation caps",
                check=Check.IS_TRUE,
                field="endurance.logs_within_caps",
            ),
        ),
        time_budget_s=2400,
        logs=(_CONSOLE, _SIDECAR_LOG, _SIDECAR_JSONL, _ACKS, _EVENTS, _QUEUE),
        suspect_module="policy/autonomy.py, pz_agent_cli.runtime (tick budget)",
        measures_latency=True,
        screenshots_required=True,
    ),
    LiveScenario(
        id="S20_AUTONOMOUS_2_HOURS",
        title="Two hours autonomous with no drift",
        goal=(
            "Prove nothing grows without bound over a long run: memory, journals, "
            "reference generations and latency all stay flat."
        ),
        world_preparation=(
            "The same secured house as S19, restocked.",
            "Take a fresh backup immediately before starting.",
        ),
        required_state=("S19 passed.", "Session armed in AUTONOMOUS."),
        command="pz-agent live-test run --scenario S20_AUTONOMOUS_2_HOURS --observations <file>",
        operator_steps=(
            "Arm in autonomous and leave the character alone for two hours.",
            "Record the sidecar's resident memory at the start and at the end.",
        ),
        expected_result=(
            "The character survives, resident memory is flat, journals rotated within "
            "their caps, and the latency distribution matches S19's."
        ),
        postconditions=(
            Postcondition(
                key="ran_full_duration",
                statement="the run lasted at least two hours",
                check=Check.AT_LEAST,
                field="endurance.duration_s",
                expected=7200,
            ),
            Postcondition(
                key="survived",
                statement="the character is alive at the end",
                check=Check.IS_TRUE,
                field="player.alive_after",
            ),
            Postcondition(
                key="memory_flat",
                statement="resident memory grew by no more than 64 MiB",
                check=Check.AT_MOST,
                field="endurance.rss_growth_bytes",
                expected=67_108_864,
            ),
            Postcondition(
                key="logs_within_caps",
                statement="the journals stayed within their rotation caps",
                check=Check.IS_TRUE,
                field="endurance.logs_within_caps",
            ),
            Postcondition(
                key="latency_stable",
                statement="p95 action latency stayed under ten seconds",
                check=Check.AT_MOST,
                field="endurance.p95_latency_ms",
                expected=10_000,
            ),
        ),
        time_budget_s=8400,
        logs=(_CONSOLE, _SIDECAR_LOG, _SIDECAR_JSONL, _ACKS, _EVENTS, _QUEUE),
        suspect_module="pz_agent_cli.runtime, memory/store.py, ipc/journal.py (rotation)",
        measures_latency=True,
        screenshots_required=True,
    ),
)

#: Run order. The scenarios build on each other — S02 assumes S01's mod is
#: loaded, S20 assumes S19 — so this is the sequence ``resume`` walks.
SCENARIO_IDS: Final[tuple[str, ...]] = tuple(scenario.id for scenario in SCENARIOS)

_BY_ID: Final[Mapping[str, LiveScenario]] = {scenario.id: scenario for scenario in SCENARIOS}


class UnknownScenarioError(LookupError):
    """A scenario id that is not one of the twenty."""


def by_id(scenario_id: str) -> LiveScenario:
    """Look one scenario up by its exact id.

    Raises:
        UnknownScenarioError: naming the ids that do exist, because the id is
            typed by hand on the command line and a bare KeyError would send
            the operator to the source.
    """
    scenario = _BY_ID.get(scenario_id)
    if scenario is None:
        raise UnknownScenarioError(
            f"unknown scenario {scenario_id!r}; the twenty are: {', '.join(SCENARIO_IDS)}"
        )
    return scenario


def resolve(tokens: Any) -> tuple[LiveScenario, ...]:
    """Resolve a sequence of ids to scenarios, in run order.

    Passing ``None`` selects all twenty. Ids are matched exactly: the operator
    copies them from ``status`` output, and a fuzzy match that quietly ran a
    neighbouring scenario would put the wrong evidence in the wrong directory.
    """
    if tokens is None:
        return SCENARIOS
    wanted = {str(token).strip() for token in tokens if str(token).strip()}
    for token in sorted(wanted):
        by_id(token)
    return tuple(scenario for scenario in SCENARIOS if scenario.id in wanted)


def catalogue() -> JsonDict:
    """The whole catalogue as JSON, for generating the playbook."""
    return {
        "scenario_count": len(SCENARIOS),
        "checks": [check.value for check in Check],
        "scenarios": [scenario.to_dict() for scenario in SCENARIOS],
    }
