# Live test playbook

Twenty-two scenarios, S01 to S22, run against a real Project Zomboid Build 42.20
session on Windows. **None of them has been run.** They were written in an
environment with no game in it, so every one is `NOT_RUN`, and the runner treats
that as its initial state precisely so a scenario nobody ran cannot report a
pass.

S21 and S22 are the two irreversible rungs — a craft, and a placement — and they
carry an obstacle the other twenty do not. Both `crafting` and `building`
resolve to `experimental` on a clean scan; an experimental capability is not
usable; and the action engine refuses an unusable capability before it sends
anything. On a stock install the *write* half of each of those two scenarios
therefore cannot be sent at all, and no operator switch changes that
(`safety.disabled_capabilities` only ever subtracts). That is a finding to
record — `blocked_reason` in the observations file, and BLOCKED as the verdict —
not a reason to relax a postcondition. The read halves are exercisable on every
install and are where those two scenarios earn their keep: `pz_action_inspect_recipe`
and `pz_action_inspect_buildable` are published unconditionally, and the second
of them reports the `WOULD_TRAP_PLAYER` and `SQUARE_OCCUPIED` verdicts a
placement would meet.

This file is generated from `pz_agent_cli.livetest.scenarios`, which is also
what the runner executes. If the two ever disagree, the runner is right and this
file is stale — regenerate it rather than editing it.

## Decide the version before the first scenario

`finalize` stamps `version.PRODUCT_VERSION` into the evidence manifest, and
`scripts/check_release.py --release` refuses evidence whose product version is
not the version being released. That check is the last one a release passes, and
its remediation is *"bump version.py and everything that restates it … then
**re-run the scenarios**"*.

So the order matters and the cost of getting it wrong is the whole session:
twenty-two scenarios, a thirty-minute run and a two-hour run. If this run is
meant to certify a release, bump `version.py` and everything
`scripts/check_versions.py` lists **first**, and confirm with
`.venv/bin/python scripts/check_versions.py`. `pz-agent live-test prepare` prints
the number it will stamp, which is the last cheap moment to notice.

A live run made for any other reason needs none of this — the evidence simply
cannot certify a release, which is the honest answer rather than an obstacle.

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
run-live-tests.bat --scenario S07_NESTED_INVENTORY --observations obs.json
run-live-tests.bat --scenario S07_NESTED_INVENTORY  one of them, with nothing to observe
run-live-tests.bat                                  every pending scenario, in order
resume-live-tests.bat                               continue from the first that is not PASS
pz-agent live-test status                           the table
collect-evidence.bat --scenario S07_NESTED_INVENTORY  gather one scenario's logs, now
collect-evidence.bat                                every scenario that has run
finalize-release.bat                                build the manifest, refusing if anything is missing
```

**Collect after each scenario, not at the end of the day.** `finalize` requires
the declared logs of *every* scenario, passes included, and the files do not
survive waiting: `console.txt` is rewritten each time the game launches and the
session trace rotates, so by evening the early scenarios' logs no longer exist.
The only remedy then is to run those scenarios again.

**Only the first form can produce a PASS**, and it is the one each scenario's
own section repeats. A run with no `--observations` has nothing to observe, so
every scenario in it is recorded BLOCKED — useful for seeing the order, never
for closing anything. `--observations` describes one scenario, so it must be
given together with `--scenario`; without it the run selects every pending
scenario and refuses rather than guess which one the file is about.

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
`docs/GAME_API_VERIFICATION.md` is the list of every place the code is guessing,
and states how many that is; this line used to carry its own number and it had
rotted. (`grep -rn "Build 42:" pz-mod/` finds a few of them quickly; it is not
the whole list, and this line used to say it was.)

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
  `observations.install.mod_info_present` · is_true
- **`heartbeat_written`** — heartbeat.game.json exists in the exchange directory  
  `observations.ipc.game_heartbeat_present` · is_true  
  _Present on disk and enabled in the menu both hold for a mod that crashed during load. Only a written heartbeat proves it is running._
- **`heartbeat_advances`** — the heartbeat timestamp is newer in the second reading  
  `before/after.ipc.game_heartbeat_ms` · increased
- **`build_string`** — the detected game build is recorded  
  `observations.game.build` · observed

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S01_INSTALL",
  "game_build": "",
  "observations": {
    "game": {
      "build": null
    },
    "install": {
      "mod_info_present": null
    },
    "ipc": {
      "game_heartbeat_present": null
    }
  },
  "before": {
    "ipc": {
      "game_heartbeat_ms": null
    }
  },
  "after": {
    "ipc": {
      "game_heartbeat_ms": null
    }
  }
}
```

**Time budget:** 600 s

**Evidence:** `evidence/S01_INSTALL/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S01_INSTALL/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

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
  `observations.session.ids_agree` · is_true  
  _Two ids means the two sides resolved different exchange directories, and every subsequent command would be written where nothing reads it._
- **`mode_is_observe`** — the session attaches in OBSERVE  
  `observations.session.mode` · equals `'OBSERVE'`
- **`sidecar_heartbeat_advances`** — heartbeat.sidecar.json is newer in the second reading  
  `before/after.ipc.sidecar_heartbeat_ms` · increased
- **`game_heartbeat_advances`** — heartbeat.game.json is newer in the second reading  
  `before/after.ipc.game_heartbeat_ms` · increased

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S02_HEARTBEAT",
  "game_build": "",
  "observations": {
    "session": {
      "ids_agree": null,
      "mode": null
    }
  },
  "before": {
    "ipc": {
      "game_heartbeat_ms": null,
      "sidecar_heartbeat_ms": null
    }
  },
  "after": {
    "ipc": {
      "game_heartbeat_ms": null,
      "sidecar_heartbeat_ms": null
    }
  }
}
```

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
  `observations.arm.snapshot_mode` · equals `'ASSISTED'`  
  _The CLI publishes a request; only the snapshot proves the mod applied it._
- **`cli_agrees_when_armed`** — the CLI and the snapshot report the same mode  
  `observations.arm.cli_agrees` · is_true
- **`disarmed_in_snapshot`** — the snapshot reports OBSERVE after disarm  
  `observations.disarm.snapshot_mode` · equals `'OBSERVE'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S03_ARM_DISARM",
  "game_build": "",
  "observations": {
    "arm": {
      "cli_agrees": null,
      "snapshot_mode": null
    },
    "disarm": {
      "snapshot_mode": null
    }
  }
}
```

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
  `before/after.player.position` · changed
- **`arrived_at_target`** — the final tile equals the requested square  
  `observations.move.arrived_at_target` · is_true  
  _Position changed is not arrival. A character shoved one tile by a zombie also changed position._
- **`reason_code`** — the action result carries POSTCONDITION_MET  
  `observations.action_result.reason_code` · equals `'POSTCONDITION_MET'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

This scenario declares that it **measures latency**, so `latencies_ms` is
required as well and an empty list is not a measurement. Run `pz-agent latency
--json` after the scenario and read the `traces` array: for each command this
scenario issued, the sample is `terminal_at_ms - issued_at_ms`. Write those
numbers, and no others — the p50/p95 in `result.json` are computed from this
list, so a number nobody measured becomes a percentile nobody measured.

```json
{
  "scenario_id": "S04_MOVE",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "move": {
      "arrived_at_target": null
    }
  },
  "before": {
    "player": {
      "position": null
    }
  },
  "after": {
    "player": {
      "position": null
    }
  },
  "latencies_ms": []
}
```

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
  `observations.action_result.status` · equals `'failed'`
- **`path_reason_code`** — the reason code names the pathing failure  
  `observations.action_result.reason_code` · observed  
  _A generic INTERNAL_ERROR here would mean the adapter cannot tell a blocked path from a crash, and the operator has nothing to act on._
- **`ended_within_budget`** — the action ended within its timeout  
  `observations.action_result.duration_ms` · at_most `120000`
- **`health_unchanged`** — the character took no damage  
  `before/after.player.health` · unchanged

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S05_BLOCKED_PATH",
  "game_build": "",
  "observations": {
    "action_result": {
      "duration_ms": null,
      "reason_code": null,
      "status": null
    }
  },
  "before": {
    "player": {
      "health": null
    }
  },
  "after": {
    "player": {
      "health": null
    }
  }
}
```

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
  `observations.action_result.reason_code` · equals `'USER_TAKEOVER'`
- **`disarmed_after_takeover`** — the snapshot reports OBSERVE afterwards  
  `observations.safety.mode_after` · equals `'OBSERVE'`
- **`player_queue_untouched`** — no action the player queued was cancelled  
  `observations.safety.player_queue_intact` · is_true  
  _Cancelling the player's own queued work is the agent overriding them at the exact moment they took control._

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S06_MANUAL_TAKEOVER",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "safety": {
      "mode_after": null,
      "player_queue_intact": null
    }
  }
}
```

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
  `observations.transfer.item_in_main_after` · is_true
- **`item_left_source`** — the item is absent from the inner container after the action  
  `observations.transfer.item_in_source_after` · is_false  
  _Present in both would mean the reference resolved to a different object of the same type, which is exactly the nesting bug._
- **`main_count_increased`** — the main inventory item count went up  
  `before/after.inventory.main_count` · increased
- **`reason_code`** — the action result carries POSTCONDITION_MET  
  `observations.action_result.reason_code` · equals `'POSTCONDITION_MET'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S07_NESTED_INVENTORY",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "transfer": {
      "item_in_main_after": null,
      "item_in_source_after": null
    }
  },
  "before": {
    "inventory": {
      "main_count": null
    }
  },
  "after": {
    "inventory": {
      "main_count": null
    }
  }
}
```

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
  `observations.selection.chosen_is_safe` · is_true
- **`rotten_untouched`** — the rotten item is still in the inventory afterwards  
  `observations.selection.rotten_still_present` · is_true
- **`hunger_fell`** — hunger is lower after than before  
  `before/after.player.hunger` · decreased  
  _Eating that does not change hunger is indistinguishable from not eating._
- **`rationale_recorded`** — the selection rationale is recorded  
  `observations.selection.rationale` · observed

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S08_UNSAFE_FOOD",
  "game_build": "",
  "observations": {
    "selection": {
      "chosen_is_safe": null,
      "rationale": null,
      "rotten_still_present": null
    }
  },
  "before": {
    "player": {
      "hunger": null
    }
  },
  "after": {
    "player": {
      "hunger": null
    }
  }
}
```

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
  `before/after.player.thirst` · decreased
- **`source_is_safe`** — the chosen source was not tainted  
  `observations.selection.source_is_safe` · is_true
- **`chosen_ref`** — the chosen source reference is recorded  
  `observations.selection.chosen_ref` · observed

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S09_DRINK",
  "game_build": "",
  "observations": {
    "selection": {
      "chosen_ref": null,
      "source_is_safe": null
    }
  },
  "before": {
    "player": {
      "thirst": null
    }
  },
  "after": {
    "player": {
      "thirst": null
    }
  }
}
```

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
  `observations.action_result.evidence.reading_started` · is_true
- **`progress_advanced`** — pages read is higher after than before  
  `before/after.literature.pages_read` · increased  
  _Progress is the postcondition; starting is not._
- **`book_matches_goal`** — the chosen book matches the stated skill goal  
  `observations.selection.matches_goal` · is_true

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S10_READ",
  "game_build": "",
  "observations": {
    "action_result": {
      "evidence": {
        "reading_started": null
      }
    },
    "selection": {
      "matches_goal": null
    }
  },
  "before": {
    "literature": {
      "pages_read": null
    }
  },
  "after": {
    "literature": {
      "pages_read": null
    }
  }
}
```

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
  `observations.container.ref` · observed  
  _A world container reference carries colons in its tail; a naive split yields a valid reference to a different object._
- **`contents_match`** — the reported contents match the in-game panel  
  `observations.container.contents_match_panel` · is_true
- **`item_count`** — at least three items were listed  
  `observations.container.item_count` · at_least `3`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S11_CONTAINER",
  "game_build": "",
  "observations": {
    "container": {
      "contents_match_panel": null,
      "item_count": null,
      "ref": null
    }
  }
}
```

**Time budget:** 360 s

**Evidence:** `evidence/S11_CONTAINER/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S11_CONTAINER/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

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
  `observations.equipment.primary_matches_request` · is_true
- **`hands_changed`** — the hands view differs between before and after  
  `before/after.player.hands` · changed
- **`reason_code`** — the action result carries POSTCONDITION_MET  
  `observations.action_result.reason_code` · equals `'POSTCONDITION_MET'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S12_EQUIPMENT",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "equipment": {
      "primary_matches_request": null
    }
  },
  "before": {
    "player": {
      "hands": null
    }
  },
  "after": {
    "player": {
      "hands": null
    }
  }
}
```

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
  `observations.medical.wound_bandaged_after` · is_true
- **`untreated_count_fell`** — the number of untreated wounds went down  
  `before/after.player.untreated_wounds` · decreased
- **`panel_agrees`** — the in-game health panel agrees with the reported state  
  `observations.medical.panel_agrees` · is_true

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S13_MEDICAL",
  "game_build": "",
  "observations": {
    "medical": {
      "panel_agrees": null,
      "wound_bandaged_after": null
    }
  },
  "before": {
    "player": {
      "untreated_wounds": null
    }
  },
  "after": {
    "player": {
      "untreated_wounds": null
    }
  }
}
```

**Time budget:** 420 s

**Evidence:** `evidence/S13_MEDICAL/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S13_MEDICAL/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

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
  `before/after.player.fatigue` · decreased
- **`stop_honoured`** — the stop request ended the action  
  `observations.action_result.reason_code` · equals `'CANCELLED_BY_REQUEST'`
- **`awake_after`** — the character is awake afterwards  
  `observations.player.awake_after` · is_true

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S14_SLEEP_REST",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "player": {
      "awake_after": null
    }
  },
  "before": {
    "player": {
      "fatigue": null
    }
  },
  "after": {
    "player": {
      "fatigue": null
    }
  }
}
```

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
  `observations.action_result.reason_code` · equals `'THREAT_INTERRUPTED'`
- **`zombie_observed`** — the nearby zombie appears in the snapshot  
  `observations.threat.zombies_seen` · at_least `1`  
  _An interrupt with no zombie in the snapshot means the guard fired on something else and the reason code is wrong._
- **`disarmed`** — the snapshot reports OBSERVE afterwards  
  `observations.safety.mode_after` · equals `'OBSERVE'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S15_ZOMBIE_INTERRUPT",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "safety": {
      "mode_after": null
    },
    "threat": {
      "zombies_seen": null
    }
  }
}
```

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
  `observations.safety.mode_after` · equals `'OBSERVE'`
- **`staleness_detected`** — the mod reports the sidecar heartbeat as stale  
  `observations.session.sidecar_stale` · is_true
- **`no_action_after`** — no command was executed after the sidecar went silent  
  `observations.session.acks_after_silence` · equals `0`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S16_STALE_SIDECAR",
  "game_build": "",
  "observations": {
    "safety": {
      "mode_after": null
    },
    "session": {
      "acks_after_silence": null,
      "sidecar_stale": null
    }
  }
}
```

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
  `observations.session.reattached` · is_true
- **`no_replay`** — no command id from before the restart was acked again  
  `observations.session.replayed_command_count` · equals `0`  
  _A replay after restart would repeat a real in-game action — eating the last safe food twice, for instance._
- **`mode_observe`** — the restarted sidecar attaches in OBSERVE  
  `observations.session.mode` · equals `'OBSERVE'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S17_RESTART_RECOVERY",
  "game_build": "",
  "observations": {
    "session": {
      "mode": null,
      "reattached": null,
      "replayed_command_count": null
    }
  }
}
```

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
  `observations.action_result.reason_code` · equals `'PANIC_STOP'`
- **`player_action_survived`** — the player's queued action is still present afterwards  
  `observations.safety.player_action_survived` · is_true
- **`disarmed`** — the snapshot reports OBSERVE afterwards  
  `observations.safety.mode_after` · equals `'OBSERVE'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S18_PANIC",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "safety": {
      "mode_after": null,
      "player_action_survived": null
    }
  }
}
```

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
  `observations.endurance.duration_s` · at_least `1800`
- **`stayed_in_radius`** — the character never left the configured radius  
  `observations.autonomy.stayed_in_radius` · is_true
- **`single_action_at_a_time`** — never more than one agent action was in flight  
  `observations.autonomy.max_concurrent_actions` · at_most `1`
- **`survived`** — the character is alive at the end  
  `observations.player.alive_after` · is_true
- **`no_unbounded_growth`** — the journals stayed within their rotation caps  
  `observations.endurance.logs_within_caps` · is_true

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

This scenario declares that it **measures latency**, so `latencies_ms` is
required as well and an empty list is not a measurement. Run `pz-agent latency
--json` after the scenario and read the `traces` array: for each command this
scenario issued, the sample is `terminal_at_ms - issued_at_ms`. Write those
numbers, and no others — the p50/p95 in `result.json` are computed from this
list, so a number nobody measured becomes a percentile nobody measured.

```json
{
  "scenario_id": "S19_AUTONOMOUS_30_MIN",
  "game_build": "",
  "observations": {
    "autonomy": {
      "max_concurrent_actions": null,
      "stayed_in_radius": null
    },
    "endurance": {
      "duration_s": null,
      "logs_within_caps": null
    },
    "player": {
      "alive_after": null
    }
  },
  "latencies_ms": []
}
```

**Time budget:** 2400 s  ·  **latency measured** (p50/p95 recorded in `result.json`)

**Evidence:** `evidence/S19_AUTONOMOUS_30_MIN/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S19_AUTONOMOUS_30_MIN/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

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
  `observations.endurance.duration_s` · at_least `7200`
- **`survived`** — the character is alive at the end  
  `observations.player.alive_after` · is_true
- **`memory_flat`** — resident memory grew by no more than 64 MiB  
  `observations.endurance.rss_growth_bytes` · at_most `67108864`
- **`logs_within_caps`** — the journals stayed within their rotation caps  
  `observations.endurance.logs_within_caps` · is_true
- **`latency_stable`** — p95 action latency stayed under ten seconds  
  `observations.endurance.p95_latency_ms` · at_most `10000`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

This scenario declares that it **measures latency**, so `latencies_ms` is
required as well and an empty list is not a measurement. Run `pz-agent latency
--json` after the scenario and read the `traces` array: for each command this
scenario issued, the sample is `terminal_at_ms - issued_at_ms`. Write those
numbers, and no others — the p50/p95 in `result.json` are computed from this
list, so a number nobody measured becomes a percentile nobody measured.

```json
{
  "scenario_id": "S20_AUTONOMOUS_2_HOURS",
  "game_build": "",
  "observations": {
    "endurance": {
      "duration_s": null,
      "logs_within_caps": null,
      "p95_latency_ms": null,
      "rss_growth_bytes": null
    },
    "player": {
      "alive_after": null
    }
  },
  "latencies_ms": []
}
```

**Time budget:** 8400 s  ·  **latency measured** (p50/p95 recorded in `result.json`)

**Evidence:** `evidence/S20_AUTONOMOUS_2_HOURS/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S20_AUTONOMOUS_2_HOURS/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

### When it fails

Look first at **pz_agent_cli.runtime, memory/store.py, ipc/journal.py (rotation)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `pz-agent.jsonl`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`
- `command.queue.0001.jsonl`

## S21_CRAFT

**Read a recipe, then craft once and observe the product**

Prove the crafting readers answer on a real Build 42.20 install — they are jointly the least certain rows in docs/GAME_API_VERIFICATION.md — and that a craft is judged by the product observed afterwards, never by the command being accepted.

### Prepare the world

- Take a backup: this is the first scenario whose work cannot be undone.
- Carry the materials for one simple recipe the character already knows, and nothing else the same recipe could consume.
- Open the in-game crafting panel and write down what that recipe lists as its ingredients and its product; that is what the reading is checked against.

### Required starting state

- Session armed in ASSISTED.
- Read pz://capabilities first and record what 'crafting' says. On a stock install it is 'experimental', pz_action_craft is withheld, and the craft half of this scenario cannot be sent — record that as blocked_reason and stop rather than reporting a pass.
- No zombies in view: a craft interrupted by the reflex guard proves nothing about the readers.

### Run

```
pz-agent live-test run --scenario S21_CRAFT --observations <file>
```

### What you do in the game

1. Read one recipe with pz_action_inspect_recipe and compare its ingredient list and product against the in-game panel, line by line. A reading that answers nothing is the failure this step exists to catch.
2. If 'crafting' is usable on this install, run that recipe exactly once and watch the inventory: the product should appear and an ingredient should fall.
3. If it is withheld, record the refusal verbatim in blocked_reason — naming the capability and its reason — and do not run anything else.

### Expected

The recipe reading matches the game's own panel, and the craft — where it could be sent at all — ends with the product observed in the inventory and an ingredient observed to have fallen. A withheld capability is a BLOCKED attempt with the reason recorded, which is a finding about this build and not a failure of the code.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`recipe_readout_present`** — the observation carried a crafting readout for the recipe  
  `observations.crafting.readout_present` · is_true  
  _Every crafting reader in the mod is a guess at a Build 42 spelling. An observation carrying no readout at all means none of them answered, which is the first thing this run exists to establish._
- **`ingredients_match_panel`** — the reported ingredients match the in-game crafting panel  
  `observations.crafting.ingredients_match_panel` · is_true  
  _A reader that answers with the wrong list is worse than one that answers nothing: the policy would call a recipe affordable on requirements nobody actually has._
- **`capability_state_recorded`** — pz://capabilities' state for 'crafting' is recorded  
  `observations.crafting.capability_state` · observed
- **`product_count_rose`** — the inventory holds more of the product after than before  
  `before/after.inventory.product_count` · increased  
  _The only postcondition a craft has. An ack saying the craft finished is a statement about the queue._
- **`ingredient_count_fell`** — an ingredient the recipe consumes is observed to have fallen  
  `before/after.inventory.ingredient_count` · decreased  
  _A product that appeared while nothing was spent did not come out of this recipe, and the mod requires this half too._
- **`reason_code`** — the action result carries POSTCONDITION_MET  
  `observations.action_result.reason_code` · equals `'POSTCONDITION_MET'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S21_CRAFT",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "crafting": {
      "capability_state": null,
      "ingredients_match_panel": null,
      "readout_present": null
    }
  },
  "before": {
    "inventory": {
      "ingredient_count": null,
      "product_count": null
    }
  },
  "after": {
    "inventory": {
      "ingredient_count": null,
      "product_count": null
    }
  }
}
```

**Time budget:** 600 s

**Evidence:** `evidence/S21_CRAFT/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S21_CRAFT/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

### When it fails

Look first at **policy/crafting.py, actions/adapters/crafting.py, PZAgent/adapters/Crafting.lua, Observe.lua (the crafting readout)**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`
- `command.queue.0001.jsonl`

## S22_BUILD

**A wall that would seal the character in is refused; a wall that would not is raised**

Prove the refusal before the placement. WOULD_TRAP_PLAYER is computed from a bounded observed window, and nothing in this build takes a structure back down — so the run that matters is the one where the agent is asked for a wall it must refuse, and the placement is only worth attempting afterwards.

### Prepare the world

- Take a fresh backup. This is the only scenario whose work stays in the world: nothing in this project removes a structure once it stands.
- Stand in a small room, alcove or corridor end with exactly ONE way out, and note the coordinates of that one square — that is the trap square.
- Note a second square on open ground with at least two ways out from where the character stands; that is the safe square.
- Carry the materials for one simple structure the character knows how to build, and open the in-game build menu to write down what it costs.

### Required starting state

- Session armed in ASSISTED.
- Read pz://capabilities first and record what 'building' says. On a stock install it is 'experimental', pz_action_build is withheld, and no placement can be sent — record that as blocked_reason. The refusal steps below still run: pz_action_inspect_buildable is published on every install and reports the verdict the build would reach.
- No zombies in view, and the character standing inside the enclosure being tested — the check starts from the square the character is on.

### Run

```
pz-agent live-test run --scenario S22_BUILD --observations <file>
```

### What you do in the game

1. Read the TRAP square with pz_action_inspect_buildable and confirm the reading refuses the solid blueprint with would_trap_player. Do this FIRST, before anything is placed anywhere.
2. If 'building' is usable on this install, ask for that wall on the trap square as well, and confirm the command is refused with WOULD_TRAP_PLAYER and that nothing was queued — walk the character out and back in to check the square is still empty.
3. Read a square that already holds something — a wall, a crate, a tree — and confirm it refuses with square_occupied and names what it found.
4. Only then read the SAFE square, confirm it reports the blueprint buildable, and — if the capability allows it — raise the structure there once.
5. Look at the square in game: the structure must be standing on it. Then stop; there is no second placement in this scenario and no way to undo the first.

### Expected

The trap square is refused with WOULD_TRAP_PLAYER and nothing is queued for it; an occupied square is refused with SQUARE_OCCUPIED naming the blocker; the safe square reads buildable and, where the capability allows a placement at all, the structure is observed standing on it afterwards. A withheld capability makes the placement half BLOCKED with the reason recorded — the two refusals are still observed and still reported.

### Required postconditions

Every one of these must be observed. A missing one is not a pass.

- **`trap_refusal_fired`** — the trap square is refused with would_trap_player  
  `observations.building.trap_square_refusal` · equals `'would_trap_player'`  
  _The heart of this rung. A wall the agent raises cannot be taken down by the agent, so a wall that seals the character in is a mistake with no undo — and the check has to fire from the live map, not only from a fixture._
- **`nothing_queued_for_the_trap_square`** — the trap square is still empty after the refusal  
  `observations.building.trap_square_still_empty` · is_true  
  _A refusal that still queued the work would be the worst outcome this scenario can produce: the answer says no and the wall goes up._
- **`occupied_refusal_fired`** — an occupied square is refused with square_occupied  
  `observations.building.occupied_square_refusal` · equals `'square_occupied'`  
  _The agent never clears a square to make room for its own placement._
- **`safe_square_reads_buildable`** — the safe square reports the blueprint as buildable  
  `observations.building.safe_square_buildable` · is_true  
  _Without this the two refusals prove only that the reading refuses everything, which would be a check that cannot tell a wall from a doorway._
- **`capability_state_recorded`** — pz://capabilities' state for 'building' is recorded  
  `observations.building.capability_state` · observed
- **`structure_observed_on_the_square`** — the structure is observed standing on the safe square afterwards  
  `observations.building.structure_observed_after` · is_true  
  _The only postcondition a build has. A queued build is not a wall, and an ack that says the action finished is a statement about the queue._
- **`square_objects_grew`** — the safe square carries more objects after than before  
  `before/after.building.safe_square_objects` · increased
- **`reason_code`** — the action result carries POSTCONDITION_MET  
  `observations.action_result.reason_code` · equals `'POSTCONDITION_MET'`

### The observations file

Fill every `null` with what you read back, then pass it to `--observations`.
A value left unread fails its postcondition — that is the point.

`game_build` is required too, and it is not a `null`: write the build string
the running game reports. No postcondition reads it, so a scenario would otherwise
pass without it and the release gate would refuse the whole archive after all 22
sessions were spent. The runner refuses that PASS here instead.

```json
{
  "scenario_id": "S22_BUILD",
  "game_build": "",
  "observations": {
    "action_result": {
      "reason_code": null
    },
    "building": {
      "capability_state": null,
      "occupied_square_refusal": null,
      "safe_square_buildable": null,
      "structure_observed_after": null,
      "trap_square_refusal": null,
      "trap_square_still_empty": null
    }
  },
  "before": {
    "building": {
      "safe_square_objects": null
    }
  },
  "after": {
    "building": {
      "safe_square_objects": null
    }
  }
}
```

**Time budget:** 900 s

**Evidence:** `evidence/S22_BUILD/`  — screenshots required

**Take the screenshot while the scenario is running**, and save it into
`evidence/S22_BUILD/screenshots/`. Nothing can produce it later: `collect-evidence.bat` gathers
logs and journals and never touches that directory, and the moment it has to
show is over when the scenario ends. `finalize` refuses this scenario with
"no screenshot was collected for a scenario that requires one" — after every
session has been spent.

The runner checks only that a file is there; whether it shows the state the
postconditions describe is on you, and that is the whole reason this scenario
asks for one — it is the part a person has to look at.

### When it fails

Look first at **policy/building.py (enclosure_after), actions/adapters/building.py, PZAgent/adapters/Building.lua**.

Logs to collect:

- `console.txt`
- `pz-agent.log`
- `command.ack.0001.jsonl`
- `observation.events.0001.jsonl`
- `command.queue.0001.jsonl`

---

## Nothing here is a pass yet

Twenty `NOT_RUN` rows. `scripts/check_release.py --release` refuses to certify
v1.0.0 while `release/evidence-manifest.json` is absent, and that file is
produced by `pz-agent live-test finalize` and by nothing else — not by a build,
not by a green test suite. It is the record of what was seen in the game.
