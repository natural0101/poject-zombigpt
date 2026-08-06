# Live test playbook

Twenty scenarios, S01 to S20, run against a real Project Zomboid Build 42.20
session on Windows. **None of them has been run.** They were written in an
environment with no game in it, so every one is `NOT_RUN`, and the runner treats
that as its initial state precisely so a scenario nobody ran cannot report a
pass.

This file is generated from `pz_agent_cli.livetest.scenarios`, which is also
what the runner executes. If the two ever disagree, the runner is right and this
file is stale — regenerate it rather than editing it.

## Before the first scenario

```
install.bat
doctor.bat
backup-save.bat
```

Use a dedicated **test save**. `pz-agent live-test prepare` checks that a backup
exists and refuses to run against your main save. `pz-agent restore-save`
refuses while the game is open, which is what stops a restore from destroying
the save it is restoring.

## Running them

```
run-live-tests.bat                                  every pending scenario, in order
run-live-tests.bat --scenario S07_NESTED_INVENTORY  one of them
resume-live-tests.bat                               continue from the first that is not PASS
pz-agent live-test status                           the table
collect-evidence.bat                                gather logs into the scenario folders
finalize-release.bat                                build the manifest, refusing if anything is missing
```

## What PASS means here

A scenario passes when its **postconditions were observed**, not when the run
finished without an error. The runner writes `result.json` itself, records a
content hash, and verifies that hash on read — there is no command to edit a
result, and `finalize` detects one that was edited by hand.

If a scenario will not pass, the fix is in the code. Relaxing a postcondition,
catching an exception and calling it success, or marking a scenario passed by
hand all produce the same thing: a release whose evidence proves nothing.

Read `docs/LOCAL_DEBUG_MAP.md` when one fails. Nearly every first-run failure is
a Build 42.20 API spelled differently from what the adapter declared, and
`grep -rn "Build 42:" pz-mod/` is the list of every place the code is guessing.

---
## S01_INSTALL

**Mod installs, loads and is visible from both sides**

Prove the bridge mod is executing inside Build 42.20, not merely present on disk — the failure this catches is a mod that installed cleanly and threw during load, which looks identical to an idle exchange directory.

### Prepare the world

- Create a dedicated test world; do not open the main save.
- Take a backup with 'pz-agent backup-save' before the first launch.

### Required starting state

- Project Zomboid closed.
- No sidecar running.

### Run

```
pz-agent install-mod && pz-agent doctor --json
```

### What you do in the game

1. Run install-mod, then launch the game.
2. Enable 'PZ Agent Bridge' in the Mods screen and load the test save.
3. Leave the game on the loaded save and run doctor again.

### Expected

doctor resolves the install, the Zomboid directory and the exchange directory, and the game heartbeat file exists with an advancing timestamp.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`mod_files_on_disk`** — mod.info exists in the Zomboid mods directory
- **`heartbeat_written`** — heartbeat.game.json exists in the exchange directory  
  _Present on disk and enabled in the menu both hold for a mod that crashed during load. Only a written heartbeat proves it is running._
- **`heartbeat_advances`** — the heartbeat timestamp is newer in the second reading
- **`build_string`** — the detected game build is recorded

**Time budget:** 600 s

**Evidence:** `evidence/S01_INSTALL/`  — screenshots required

### When it fails

Look first at **pz-mod/42/media/lua/client/PZAgent/ (load order), pz_agent_cli.modinstall**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `heartbeat.game.json`

## S02_HEARTBEAT

**Session handshake and two-way heartbeat**

Prove the sidecar and the mod agree on one session id and each sees the other's liveness, which every later scenario assumes.

### Prepare the world

- Test save loaded and standing still indoors.

### Required starting state

- Game running with the save loaded.
- Mod heartbeat already advancing (S01 passed).

### Run

```
pz-agent start && pz-agent status --json
```

### What you do in the game

1. Start the sidecar and leave the character idle for one minute.
2. Run status twice, roughly thirty seconds apart.

### Expected

status reports the mod present, one session id shared by both sides, mode OBSERVE, and both heartbeats advancing.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`session_id_agreed`** — the sidecar and the snapshot name the same session id  
  _Two ids means the two sides resolved different exchange directories, and every subsequent command would be written where nothing reads it._
- **`mode_is_observe`** — the session attaches in OBSERVE
- **`sidecar_heartbeat_advances`** — heartbeat.sidecar.json is newer in the second reading
- **`game_heartbeat_advances`** — heartbeat.game.json is newer in the second reading

**Time budget:** 300 s

**Evidence:** `evidence/S02_HEARTBEAT/`

### When it fails

Look first at **pz_agent_core.session, PZAgent/Session.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `heartbeat.game.json`
- `observation.events.0001.jsonl`

## S03_ARM_DISARM

**Arming grants authority and disarming takes it back**

Prove the mode transition is applied by the mod and not only claimed by the CLI: the snapshot's safety.mode is the value the game is acting on.

### Prepare the world

- Character idle and safe, no zombies in view.

### Required starting state

- Sidecar attached in OBSERVE (S02 passed).

### Run

```
pz-agent arm --mode assisted && pz-agent status --json && pz-agent disarm
```

### What you do in the game

1. Arm in assisted mode, read status, then disarm and read status again.

### Expected

safety.mode in the snapshot goes OBSERVE -> ASSISTED -> OBSERVE, and the CLI agrees with the snapshot at every step.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`armed_in_snapshot`** — the snapshot reports ASSISTED after arm  
  _The CLI publishes a request; only the snapshot proves the mod applied it._
- **`cli_agrees_when_armed`** — the CLI and the snapshot report the same mode
- **`disarmed_in_snapshot`** — the snapshot reports OBSERVE after disarm

**Time budget:** 300 s

**Evidence:** `evidence/S03_ARM_DISARM/`

### When it fails

Look first at **pz_agent_cli.supervisor, PZAgent/Safety.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.queue.0001.jsonl`
- `command.ack.0001.jsonl`

## S04_MOVE

**Move to a square and observe arrival**

Prove a movement command reaches the character and that success is decided by the position that was read back, not by the command being accepted.

### Prepare the world

- Stand in an open indoor room with a clear three-tile walk.
- Note the starting tile coordinates from 'pz-agent status --json'.

### Required starting state

- Session armed in ASSISTED.

### Run

```
pz-agent live-test run --scenario S04_MOVE --observations <file>
```

### What you do in the game

1. Issue a move to a square three tiles away.
2. Do not touch the keyboard while the character walks.

### Expected

The character arrives; the action result is succeeded with the arrival position as its evidence.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`position_changed`** — the player's tile differs between before and after
- **`arrived_at_target`** — the final tile equals the requested square  
  _Position changed is not arrival. A character shoved one tile by a zombie also changed position._
- **`reason_code`** — the action result carries POSTCONDITION_MET

**Time budget:** 300 s  ·  **latency measured** (p50/p95 recorded in `result.json`)

**Evidence:** `evidence/S04_MOVE/`

### When it fails

Look first at **actions/adapters/movement.py, PZAgent/adapters/Movement.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `command.queue.0001.jsonl`
- `observation.events.0001.jsonl`

## S05_BLOCKED_PATH

**An unreachable square fails with a named reason**

Prove a movement that cannot complete reports a specific failure rather than timing out into a generic error or, worse, reporting success.

### Prepare the world

- Find a square behind a locked door or across a wall.
- Note its coordinates.

### Required starting state

- Session armed in ASSISTED.
- Character not in combat.

### Run

```
pz-agent live-test run --scenario S05_BLOCKED_PATH --observations <file>
```

### What you do in the game

1. Issue a move to the unreachable square.
2. Watch until the action ends on its own; do not cancel it.

### Expected

The action fails with PATH_NOT_FOUND or PATH_STUCK and the character is unhurt.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`failed_not_succeeded`** — the action status is failed
- **`path_reason_code`** — the reason code names the pathing failure  
  _A generic INTERNAL_ERROR here would mean the adapter cannot tell a blocked path from a crash, and the operator has nothing to act on._
- **`ended_within_budget`** — the action ended within its timeout
- **`health_unchanged`** — the character took no damage

**Time budget:** 420 s

**Evidence:** `evidence/S05_BLOCKED_PATH/`

### When it fails

Look first at **actions/adapters/movement.py, ActionRuntime.lua (timeout path)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `command.queue.0001.jsonl`

## S06_MANUAL_TAKEOVER

**Moving by hand cancels the agent's action**

Prove the user always wins: a keypress during an agent action ends that action and returns the session to OBSERVE.

### Prepare the world

- A long clear walk, at least ten tiles.

### Required starting state

- Session armed in ASSISTED.

### Run

```
pz-agent live-test run --scenario S06_MANUAL_TAKEOVER --observations <file>
```

### What you do in the game

1. Issue a move to a square ten tiles away.
2. After roughly two seconds, press a movement key yourself.
3. Keep moving manually for a few seconds, then stop.

### Expected

The in-flight action ends as USER_TAKEOVER, the session drops to OBSERVE, and nothing the player queued was cancelled.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`takeover_reason`** — the action result reason code is USER_TAKEOVER
- **`disarmed_after_takeover`** — the snapshot reports OBSERVE afterwards
- **`player_queue_untouched`** — no action the player queued was cancelled  
  _Cancelling the player's own queued work is the agent overriding them at the exact moment they took control._

**Time budget:** 420 s

**Evidence:** `evidence/S06_MANUAL_TAKEOVER/`

### When it fails

Look first at **PZAgent/Safety.lua (manual takeover), Ownership.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S07_NESTED_INVENTORY

**Transfer an item out of a bag inside a bag**

Prove references survive nesting: the item is identified inside a container that is itself inside a container, and it lands in the main inventory.

### Prepare the world

- Put a backpack inside another backpack.
- Put one uniquely named item in the inner backpack.
- Wear the outer backpack.

### Required starting state

- Session armed in ASSISTED.
- Main inventory has free capacity.

### Run

```
pz-agent live-test run --scenario S07_NESTED_INVENTORY --observations <file>
```

### What you do in the game

1. Issue a transfer of the named item to the main inventory.
2. Do not open or close any container by hand while it runs.

### Expected

The item is in the main inventory afterwards and gone from the inner bag.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`item_in_main`** — the item is present in the main inventory after the action
- **`item_left_source`** — the item is absent from the inner container after the action  
  _Present in both would mean the reference resolved to a different object of the same type, which is exactly the nesting bug._
- **`main_count_increased`** — the main inventory item count went up
- **`reason_code`** — the action result carries POSTCONDITION_MET

**Time budget:** 420 s

**Evidence:** `evidence/S07_NESTED_INVENTORY/`

### When it fails

Look first at **actions/adapters/inventory.py, protocol/refs.py, PZAgent/Refs.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `command.queue.0001.jsonl`

## S08_UNSAFE_FOOD

**Rotten food is refused and safe food is chosen**

Prove the deterministic food policy runs against real item data: the rotten item is never selected, and the hunger drop is observed rather than assumed.

### Prepare the world

- Place one rotten food item and one fresh food item in the main inventory.
- Let the character get hungry (skip a meal or two).

### Required starting state

- Session armed in ASSISTED.
- Hunger clearly above zero.

### Run

```
pz-agent live-test run --scenario S08_UNSAFE_FOOD --observations <file>
```

### What you do in the game

1. Ask the agent to eat.
2. Watch which item it picks in the inventory panel.

### Expected

The fresh item is eaten, the rotten one is untouched, and hunger falls.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`chosen_is_safe`** — the chosen item is the fresh one
- **`rotten_untouched`** — the rotten item is still in the inventory afterwards
- **`hunger_fell`** — hunger is lower after than before  
  _Eating that does not change hunger is indistinguishable from not eating._
- **`rationale_recorded`** — the selection rationale is recorded

**Time budget:** 420 s

**Evidence:** `evidence/S08_UNSAFE_FOOD/`

### When it fails

Look first at **policy/food.py, actions/adapters/consume.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S09_DRINK

**Drink from a safe source and observe thirst fall**

Prove drink selection prefers a safe source over tainted water and that thirst is read back, not inferred from the action starting.

### Prepare the world

- Provide a water bottle and, if available, a tainted source nearby.
- Stand within sight of a sink, well or rain collector if the save has one; that is the only way consume.drink_source can be confirmed.
- Let the character get thirsty.

### Required starting state

- Session armed in ASSISTED.
- Thirst clearly above zero.

### Run

```
pz-agent live-test run --scenario S09_DRINK --observations <file>
```

### What you do in the game

1. Ask the agent to drink.
2. Note the source it used.
3. If a world water source is in view, run the drink again naming it, and check that the vessel was filled rather than emptied — the argument order of ISTakeWaterAction is unconfirmed and a wrong order fills the wrong thing without erroring. See docs/GAME_API_VERIFICATION.md.

### Expected

The safe source is used and thirst falls. If a world source was named, drink_world_source leaves 'experimental' and reaches 'verified'; if none was in reach it stays experimental, which is a pass for this scenario and an unconfirmed capability in the report.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`thirst_fell`** — thirst is lower after than before
- **`source_is_safe`** — the chosen source was not tainted
- **`chosen_ref`** — the chosen source reference is recorded

**Time budget:** 360 s

**Evidence:** `evidence/S09_DRINK/`

### When it fails

Look first at **policy/selection.py (drink), actions/adapters/consume.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`

## S10_READ

**Read literature with observed progress**

Prove reading starts and advances. A read that starts and never progresses is indistinguishable from one that failed silently.

### Prepare the world

- Put a skill book the character can actually use in the inventory.
- Sit somewhere safe and lit.

### Required starting state

- Session armed in ASSISTED.
- Character is not Illiterate and meets the book's skill requirement.

### Run

```
pz-agent live-test run --scenario S10_READ --observations <file>
```

### What you do in the game

1. Ask the agent to read toward a stated skill goal.
2. Let it read for at least one minute of game time.

### Expected

Reading starts, pages read advances, and the book matches the stated goal.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`reading_started`** — the reading action reported started
- **`progress_advanced`** — pages read is higher after than before  
  _Progress is the postcondition; starting is not._
- **`book_matches_goal`** — the chosen book matches the stated skill goal

**Time budget:** 600 s

**Evidence:** `evidence/S10_READ/`

### When it fails

Look first at **policy/literature.py, actions/adapters/literature.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S11_CONTAINER

**Open a world container and read its contents**

Prove a world container reference resolves to the container the operator opened, and that its listed contents match what the game shows.

### Prepare the world

- Find a crate or counter with at least three distinguishable items.
- Stand within reach of it.

### Required starting state

- Session armed in ASSISTED.

### Run

```
pz-agent live-test run --scenario S11_CONTAINER --observations <file>
```

### What you do in the game

1. Ask the agent to inspect the nearby container.
2. Compare the reported contents against the in-game panel, item by item.

### Expected

The reported contents match the panel exactly.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`container_ref`** — the resolved container reference is recorded  
  _A world container reference carries colons in its tail; a naive split yields a valid reference to a different object._
- **`contents_match`** — the reported contents match the in-game panel
- **`item_count`** — at least three items were listed

**Time budget:** 360 s

**Evidence:** `evidence/S11_CONTAINER/`  — screenshots required

### When it fails

Look first at **actions/adapters/container.py, protocol/refs.py, PZAgent/Refs.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S12_EQUIPMENT

**Equip an item into the correct slot**

Prove an equip is verified by reading the slot back, not by the call returning.

### Prepare the world

- Put one weapon and one bag in the main inventory, hands empty.

### Required starting state

- Session armed in ASSISTED.
- Both hands free.

### Run

```
pz-agent live-test run --scenario S12_EQUIPMENT --observations <file>
```

### What you do in the game

1. Ask the agent to equip the weapon in the primary hand.

### Expected

The weapon is in the primary hand afterwards, and the slot reads back with its reference.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`slot_occupied`** — the primary hand holds the requested item afterwards
- **`hands_changed`** — the hands view differs between before and after
- **`reason_code`** — the action result carries POSTCONDITION_MET

**Time budget:** 300 s

**Evidence:** `evidence/S12_EQUIPMENT/`

### When it fails

Look first at **actions/adapters/equipment.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`

## S13_MEDICAL

**Treat a wound and observe the wound state change**

Prove medical treatment is decided by the wound list read back afterwards. This is the scenario where a fabricated success would be most harmful.

### Prepare the world

- Take a minor scratch or laceration deliberately (a fall, not a zombie).
- Carry bandages.

### Required starting state

- Session armed in ASSISTED.
- Exactly one untreated wound.

### Run

```
pz-agent live-test run --scenario S13_MEDICAL --observations <file>
```

### What you do in the game

1. Ask the agent to treat the wound.
2. Open the health panel and confirm what changed.

### Expected

The wound is bandaged, and the health panel agrees with the reported state.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`wound_bandaged`** — the wound reads as bandaged after the action
- **`untreated_count_fell`** — the number of untreated wounds went down
- **`panel_agrees`** — the in-game health panel agrees with the reported state

**Time budget:** 420 s

**Evidence:** `evidence/S13_MEDICAL/`  — screenshots required

### When it fails

Look first at **policy/medical.py, actions/adapters/medical.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S14_SLEEP_REST

**Rest reduces fatigue and is interruptible**

Prove a long self-directed action progresses, reports fatigue falling, and can be stopped on request.

### Prepare the world

- Secure a room with a bed; close and, if possible, barricade the door.
- Let the character get tired.

### Required starting state

- Session armed in ASSISTED.
- Fatigue clearly above zero.
- No zombies in view.

### Run

```
pz-agent live-test run --scenario S14_SLEEP_REST --observations <file>
```

### What you do in the game

1. Ask the agent to rest.
2. After a while, ask it to stop, and confirm the character wakes.

### Expected

Fatigue falls, and the stop request ends the action as CANCELLED_BY_REQUEST.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`fatigue_fell`** — fatigue is lower after than before
- **`stop_honoured`** — the stop request ended the action
- **`awake_after`** — the character is awake afterwards

**Time budget:** 900 s

**Evidence:** `evidence/S14_SLEEP_REST/`

### When it fails

Look first at **actions/adapters/survival.py, ActionRuntime.lua (poll loop)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S15_ZOMBIE_INTERRUPT

**A zombie interrupts a running action**

Prove the reflex guard sees a real threat and ends the in-flight action before it completes.

### Prepare the world

- Lure one zombie into an adjacent room and close the door.
- Start with the character safe and the door shut.

### Required starting state

- Session armed in ASSISTED.
- Exactly one zombie nearby, contained.

### Run

```
pz-agent live-test run --scenario S15_ZOMBIE_INTERRUPT --observations <file>
```

### What you do in the game

1. Start a long action (reading or resting).
2. Open the door so the zombie reaches the character.
3. Deal with the zombie yourself once the action has ended.

### Expected

The action ends as THREAT_INTERRUPTED and the session drops to OBSERVE.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`threat_reason`** — the action ended with THREAT_INTERRUPTED
- **`zombie_observed`** — the nearby zombie appears in the snapshot  
  _An interrupt with no zombie in the snapshot means the guard fired on something else and the reason code is wrong._
- **`disarmed`** — the snapshot reports OBSERVE afterwards

**Time budget:** 600 s

**Evidence:** `evidence/S15_ZOMBIE_INTERRUPT/`

### When it fails

Look first at **safety/reflex.py, PZAgent/Safety.lua (threat scan)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S16_STALE_SIDECAR

**The mod stops acting when the sidecar goes silent**

Prove the mod does not keep acting when nothing is supervising it. This is the safety property that makes an unattended crash survivable.

### Prepare the world

- Character safe indoors, door closed.

### Required starting state

- Session armed in ASSISTED.
- No action in flight.

### Run

```
pz-agent live-test run --scenario S16_STALE_SIDECAR --observations <file>
```

### What you do in the game

1. Kill the sidecar process — close its window, do not run 'pz-agent stop'.
2. Watch the game for the heartbeat timeout to elapse.
3. Read the snapshot afterwards.

### Expected

The mod disarms itself and reports the sidecar heartbeat as stale.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`mod_disarmed_itself`** — the snapshot reports OBSERVE after the timeout
- **`staleness_detected`** — the mod reports the sidecar heartbeat as stale
- **`no_action_after`** — no command was executed after the sidecar went silent

**Time budget:** 600 s

**Evidence:** `evidence/S16_STALE_SIDECAR/`

### When it fails

Look first at **PZAgent/Session.lua (heartbeat staleness), Safety.lua**.

Logs to collect:

- `console.txt`
- `command.ack.0001.jsonl`
- `heartbeat.game.json`
- `observation.events.0001.jsonl`

## S17_RESTART_RECOVERY

**Restarting the sidecar recovers rather than replays**

Prove restart recovery reads where it left off: no command is executed twice, and the new session is cleanly minted.

### Prepare the world

- Character safe indoors.

### Required starting state

- A completed action in the journal from an earlier scenario.

### Run

```
pz-agent start && pz-agent status --json
```

### What you do in the game

1. Note the last ack sequence number.
2. Stop the sidecar and start it again with the game still running.
3. Read status and the ack journal.

### Expected

The sidecar reattaches, no earlier command is re-executed, and the mode is OBSERVE.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`reattached`** — the sidecar reports the mod present after restart
- **`no_replay`** — no command id from before the restart was acked again  
  _A replay after restart would repeat a real in-game action — eating the last safe food twice, for instance._
- **`mode_observe`** — the restarted sidecar attaches in OBSERVE

**Time budget:** 420 s

**Evidence:** `evidence/S17_RESTART_RECOVERY/`

### When it fails

Look first at **pz_agent_cli.runtime (recovery), ipc/journal.py**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `pz-agent.jsonl`
- `command.ack.0001.jsonl`
- `command.queue.0001.jsonl`

## S18_PANIC

**Panic stop clears only what the mod owns**

Prove panic stop cancels the agent's timed actions and leaves the player's own queued work alone.

### Prepare the world

- A long task the player can queue by hand nearby.

### Required starting state

- Session armed in ASSISTED.

### Run

```
pz-agent disarm
```

### What you do in the game

1. Queue a long action yourself by hand.
2. Have the agent start a long action of its own.
3. Trigger the panic stop.

### Expected

The agent's action ends as PANIC_STOP, the session is OBSERVE, and the player's queued action is still running.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`panic_reason`** — the agent's action ended with PANIC_STOP
- **`player_action_survived`** — the player's queued action is still present afterwards
- **`disarmed`** — the snapshot reports OBSERVE afterwards

**Time budget:** 420 s

**Evidence:** `evidence/S18_PANIC/`

### When it fails

Look first at **PZAgent/Ownership.lua, Safety.lua (panic latch)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`

## S19_AUTONOMOUS_30_MIN

**Thirty minutes autonomous inside the bounds**

Prove the autonomous loop stays inside its radius, re-observes between actions, and never runs two actions at once, for half an hour.

### Prepare the world

- Secure a house with food, water and a bed.
- Set the anchor to that house and the radius to its block.
- Take a fresh backup immediately before starting.

### Required starting state

- Session armed in AUTONOMOUS.
- No zombies inside the perimeter.

### Run

```
pz-agent live-test run --scenario S19_AUTONOMOUS_30_MIN --observations <file>
```

### What you do in the game

1. Arm in autonomous and leave the character alone for thirty minutes.
2. Watch; intervene only if the save is at risk.

### Expected

The character survives, stays inside the radius, and every action is followed by a re-observation.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`ran_full_duration`** — the run lasted at least thirty minutes
- **`stayed_in_radius`** — the character never left the configured radius
- **`single_action_at_a_time`** — never more than one agent action was in flight
- **`survived`** — the character is alive at the end
- **`no_unbounded_growth`** — the journals stayed within their rotation caps

**Time budget:** 2400 s  ·  **latency measured** (p50/p95 recorded in `result.json`)

**Evidence:** `evidence/S19_AUTONOMOUS_30_MIN/`  — screenshots required

### When it fails

Look first at **policy/autonomy.py, pz_agent_cli.runtime (tick budget)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `pz-agent.jsonl`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`
- `command.queue.0001.jsonl`

## S20_AUTONOMOUS_2_HOURS

**Two hours autonomous with no drift**

Prove nothing grows without bound over a long run: memory, journals, reference generations and latency all stay flat.

### Prepare the world

- The same secured house as S19, restocked.
- Take a fresh backup immediately before starting.

### Required starting state

- S19 passed.
- Session armed in AUTONOMOUS.

### Run

```
pz-agent live-test run --scenario S20_AUTONOMOUS_2_HOURS --observations <file>
```

### What you do in the game

1. Arm in autonomous and leave the character alone for two hours.
2. Record the sidecar's resident memory at the start and at the end.

### Expected

The character survives, resident memory is flat, journals rotated within their caps, and the latency distribution matches S19's.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`ran_full_duration`** — the run lasted at least two hours
- **`survived`** — the character is alive at the end
- **`memory_flat`** — resident memory grew by no more than 64 MiB
- **`logs_within_caps`** — the journals stayed within their rotation caps
- **`latency_stable`** — p95 action latency stayed under ten seconds

**Time budget:** 8400 s  ·  **latency measured** (p50/p95 recorded in `result.json`)

**Evidence:** `evidence/S20_AUTONOMOUS_2_HOURS/`  — screenshots required

### When it fails

Look first at **pz_agent_cli.runtime, memory/store.py, ipc/journal.py (rotation)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `pz-agent.jsonl`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`
- `command.queue.0001.jsonl`

---

## Nothing here is a pass yet

Twenty `NOT_RUN` rows. `scripts/check_release.py --release` refuses to certify
v1.0.0 while `release/evidence-manifest.json` is absent, and that file is
produced by `pz-agent live-test finalize` and by nothing else — not by a build,
not by a green test suite. It is the record of what was seen in the game.
