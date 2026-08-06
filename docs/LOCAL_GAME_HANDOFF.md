# Handoff to the local game machine

This document hands the project from the remote environment — where it was
written — to a Windows machine where Project Zomboid Build 42.20 is actually
installed. It says what exists, what was proven, what was **not** proven, and
exactly how to run it.

## 1. Where the work is

| | |
|---|---|
| Repository | `natural0101/poject-zombigpt` |
| Branch | `feature/playable-agent-1.0` |
| Base | `dev` |
| Merged into `main`? | **No.** Merging is the local agent's final step, after evidence exists. |
| Tag | **None.** `v1.0.0` is deliberately not created. See §4. |
| Release candidate | `dist/pz-agent-windows-v1.0.0-rc1.zip` |

```
git fetch --all --prune
git checkout feature/playable-agent-1.0
git pull --ff-only
git rev-parse HEAD
```

Record that commit hash — every `result.json` the live-test runner writes
carries it, and a run whose commit does not match the code under test is not
evidence of anything.

## 2. What is implemented

Everything that can be implemented without a running game.

**Protocol and IPC.** A closed action whitelist of 21 names (17 of them game
actions), reason codes, session-scoped references with generation tracking, and
a file-based exchange: append-only JSONL journals for commands, acks and
observation events, plus alternating snapshot slots with the pointer written
last. Atomic rename is not guaranteed in the mod's Lua runtime, so the pointer
discipline is what makes a half-written snapshot unreadable rather than
misread.

**The mod (Lua, Build 42 target).** Heartbeat, session lifecycle, observation
builder, safety layer with panic stop and manual-takeover detection, ownership
tagging of queued timed actions, a command reader with sequence and idempotency
handling, a dispatcher that validates every argument against a declaration
before an adapter sees it, an action runtime driving one command at a time
through validate → prepare → start → poll → verify → finalize, a runtime
capability prober, and seventeen game action adapters.

**The sidecar (Python).** Full lifecycle — start, stop, arm, disarm — an IPC
loop, an observation consumer, an action engine with postcondition verification,
retries, timeouts, a circuit breaker, bounded queues and logs, and restart
recovery.

**MCP server.** Tools and resources over stdio, launched as `pz-agent-mcp`, with
ready-made client configurations under `configs/mcp/`.

**Planner.** A deterministic provider (`none`, the default, which needs no
network), an OpenAI-compatible provider for any local endpoint, and a TeamON
provider. Every plan — whatever produced it — is parsed into a typed `Plan` and
then run past the critic, capability validation, policy validation and reference
validation. The safety property is that the plan type is checked, not that the
model is trusted.

**Autonomous mode.** Hunger, thirst, fatigue, endurance, wounds, an inventory
reserve, return-to-anchor, a bounded radius, re-observation after every action,
and at most one executing step at a time.

Wired into the assembled sidecar, and worth one check before S19 or S20 because
it was *not* wired until late and the failure is silent — the character simply
stands still while every log reports a healthy sidecar:

```
python -c "import inspect, pz_agent_cli.app as a; print('planner=' in inspect.getsource(a.build_loop))"
```

`True` is what you want. `tests/contract/test_sidecar_planner_wiring.py` holds
it: removing the wiring fails eight of its assertions.

ASSISTED mode never went through the planner at all — its commands come from
MCP — so nothing above affects it.

**Three things still hold autonomy back, all by design, all visible in
`pz-agent status`.** Each answers `ASK_USER` rather than acting, which is the
safe direction, and each will make S19 and S20 look like an agent that does
nothing until it is resolved:

1. **A backup that covers *this* save.** `AutonomyGate` refuses to act unless a
   backup exists whose save id equals the one the mod reported for the open
   save — "a backup exists somewhere" is not a safety net. That id is recorded
   by `pz-agent backup-save` from the mod's own snapshot, so **take the backup
   while the game is running and the sidecar is attached**. A backup taken with
   nothing attached is complete and restorable; it simply cannot prove which
   save it covers, and `--list` marks it `no save id`. This is the one step here
   that inherently needs a live session.
2. **A capability must be *verified*, not merely available, before autonomy uses
   it.** A static scan can never reach `verified`; only a live, user-directed run
   does. So a fresh install's first autonomous meal is refused until you have
   eaten once under ASSISTED. Run the relevant scenario first — that is the
   permission ladder working, not a bug.
3. **Memory starts empty.** The store is wired, but it holds nothing until you
   put something in it, and an empty memory knows no home — so any plan needing
   a home point is refused until you set one:

   ```
   pz-agent remember home                 remembers where the character is standing
   pz-agent remember reserve Base.Beans   sets an item type aside; autonomy will not eat it
   pz-agent remember release Base.Beans
   pz-agent remember list
   ```

   All of these read the open save from the mod's own snapshot, so they need the
   game running and the sidecar attached; with nothing attached they change
   nothing and say so. A reservation takes effect on the running sidecar's next
   tick — no restart.

None of the three affects ASSISTED mode.

**Voice.** A Russian intent parser, stop on the interim transcript rather than
the final one, and a TeamON adapter, reachable through `pz-agent voice run`.
`pz-agent voice check <фраза>` resolves a phrase to an intent without a
microphone or a session, which is how you find out why a word was not
recognised.

A spoken «стоп» writes the mod's own panic latch directly — not through the
sidecar, not through the control channel — so it works whether or not a sidecar
is running, and the residual delay is the mod's next heartbeat tick. What that
call returns is that the *request* is in force, not that the game has already
stopped; the second is not observable from outside the game, and nothing here
pretends otherwise.

**A spoken goal reaches no planner.** Nothing in this build carries a goal from
a second process into the running sidecar, and writing one to the command queue
would put the microphone past the reflex guard, the capability gate and the
policy engine in a single step. So goals are refused, `status` says so, and
`voice check` says so for any goal phrase. Stopping by voice works; commanding
by voice does not.

**Windows packaging.** Self-contained executables, the BAT files listed in §6,
an installer and uninstaller, and the RC ZIP.

## 3. What was verified, and how

Everything below ran and passed in the remote environment:

- the Python test suite (unit, contract and integration) under **both**
  supported Python versions, in clean editable installs: 3435 passed and 2
  skipped, identically, on 3.11.15 and on 3.12.3. This line had been standing
  since long before voice, memory, the live-test runner and the packaging landed
  — it was re-run rather than inherited, because a claim about a build is only
  about the build it was made against;
- `mypy` in strict mode over the whole project;
- `ruff format --check` and `ruff check`;
- the mod's own Lua suite under a plain interpreter, with fake engine globals;
- `luacheck` over the mod and its tests;
- schema validity (every schema compiles as Draft 2020-12) and version sync
  across `version.py`, `pyproject.toml`, `mod.info`, the schema constants and
  the changelog;
- a forbidden-pattern scan: no stub bodies, no TODO markers, no `eval`/`exec`/
  `shell=True`/`loadstring`, and a secret scan over every tracked text file;
- the installer's full round trip against a synthetic Zomboid directory, re-run
  against the mod as it stands now rather than as it stood when the claim was
  first made — the mod has since grown from sixteen files to thirty. `install-mod`
  wrote 30 files, `uninstall-mod` removed exactly those 30, and two planted user
  files survived: one at the mod's root and one **nested inside the mod's own
  Lua tree** at `media/lua/client/PZAgent/`, which is the case a
  remove-the-directory implementation gets wrong. The save was untouched, and
  the exchange directory the mod itself writes was reported as left in place
  rather than silently deleted;
- **the release candidate as an install source**, which is what you will
  actually use: the ZIP was extracted, `install-mod --source <extracted>/mod`
  wrote the same 30 files and the same 506 613 bytes as installing from a clone,
  every installed file is byte-for-byte identical to the source tree, and all 29
  Lua files in the archive parse. The archive had been built and checksummed
  several times without anyone installing from it, which is a different claim;
- the Lua↔Python reference agreement, directly, for a world container reference
  carrying five colons — the case a naive left-to-right split resolves to a
  *different object* without erroring.

## 4. What requires a real game — and was therefore not done

This is the honest part, and the reason this handoff exists.

**There is no Project Zomboid in the environment this code was written in.** It
is a Linux container: no Windows, no Steam, no Wine, no game. So none of the
following was done, and none of it is claimed:

- **S01–S20 live scenarios.** All twenty are `NOT_RUN`. The runner's initial
  state is `NOT_RUN` precisely so that a scenario nobody ran cannot report a
  pass.
- **The 30-minute and 2-hour endurance runs.**
- **Measured p50/p95 latencies.** Any number here would have been invented.
- **Build 42.20 API signatures.** Every engine symbol is *declared* and probed;
  none is confirmed. `grep -rn "Build 42:" pz-mod/` lists every guess.
- **`v1.0.0` and its GitHub Release.** `scripts/check_release.py --release`
  fails today because `release/evidence-manifest.json` does not exist. That
  failure is the gate working.

The RC ZIP is built and its SHA-256 is printed by the build. That hash covers
the artefact, not any claim about it having been run in a game.

## 5. Installing

From the RC ZIP (no Python, no git needed):

```
install.bat
doctor.bat
```

From a clone, for development:

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pz-agent doctor
.venv\Scripts\pz-agent install-mod
```

`install-mod` copies the bridge mod into the Zomboid mods directory and records
exactly what it wrote, so `uninstall-mod` removes those files and nothing else.
A file you put inside the mod directory yourself survives an uninstall.

## 6. Running

| Step | BAT | CLI |
|---|---|---|
| Check the install | `doctor.bat` | `pz-agent doctor` |
| Back up the save | `backup-save.bat` | `pz-agent backup-save` |
| Install the mod | (part of `install.bat`) | `pz-agent install-mod` |
| Start the sidecar | `start.bat` | `pz-agent start` |
| See the state | `status.bat` | `pz-agent status` |
| Grant authority | — | `pz-agent arm --mode assisted` |
| Take it back | — | `pz-agent disarm` |
| Stop | `stop.bat` | `pz-agent stop` |
| Run the scenarios | `run-live-tests.bat` | `pz-agent live-test run` |
| Continue a run | `resume-live-tests.bat` | `pz-agent live-test resume` |
| Gather evidence | `collect-evidence.bat` | `pz-agent live-test collect` |
| Build the manifest | `finalize-release.bat` | `pz-agent live-test finalize` |

The sidecar attaches in **OBSERVE** mode. It watches and plans but cannot act
until `arm`, and `arm` is deliberately not wired to a double-clickable BAT.

Order matters: start the game and load the test save **before** `start.bat`, so
the mod has minted a session the sidecar can attach to.

## 7. Paths

Everything below assumes the default Windows layout. `pz-agent doctor` prints
the resolved values; trust it over this table if they disagree.

| What | Path |
|---|---|
| Zomboid user directory | `%USERPROFILE%\Zomboid` |
| Game log | `%USERPROFILE%\Zomboid\console.txt` |
| Saves | `%USERPROFILE%\Zomboid\Saves` |
| Mods | `%USERPROFILE%\Zomboid\mods` |
| Installed mod | `%USERPROFILE%\Zomboid\mods\PZAgent` |
| Exchange directory | printed by `pz-agent status`; under the agent's state directory |
| Sidecar logs | `<exchange>\logs` |
| Backups | under the agent's state directory, one directory per backup with a hash manifest |

Inside the exchange directory:

| File | Written by | Holds |
|---|---|---|
| `session.json` | mod | the open session and its id |
| `capabilities.json` | mod | the capability report and its revision |
| `observation.events.0001.jsonl` | mod | the observation event stream |
| `observation.snapshot.a.json`, `.b.json` | mod | alternating full snapshots |
| `observation.snapshot.pointer` | mod | which slot is current — **written last** |
| `command.queue.0001.jsonl` | sidecar | commands |
| `command.ack.0001.jsonl` | mod | acks, including evidence |
| `heartbeat.game.json` | mod | mod liveness |
| `heartbeat.sidecar.json` | sidecar | sidecar liveness |
| `panic.stop` | either | the panic latch |
| `sidecar.lock` | sidecar | single-instance lock |

## 8. The in-game console

The debug console is not needed for normal operation, and the mod does not
require debug mode.

What you do need is `console.txt`. It is written on every launch, it is where
every Lua error from the mod lands, and it is the only place a mod-side crash is
visible — a mod that has crashed is a mod that has stopped writing to the
exchange directory, so the exchange directory will simply look idle.

```
type "%USERPROFILE%\Zomboid\console.txt"
```

Read it from the top after a failure: load-time errors are at the beginning, and
they are the ones that explain why nothing at all happened.

## 9. Did the mod load?

Three checks, in increasing strength:

1. **On disk:** `%USERPROFILE%\Zomboid\mods\PZAgent\mod.info` exists.
2. **Enabled:** the game's Mods screen lists it as active. Present on disk and
   not enabled is the single most common "nothing happens".
3. **Actually running:** `heartbeat.game.json` exists in the exchange directory
   and its timestamp is advancing. This is the only one that proves the mod is
   executing rather than merely installed.

If (1) and (2) hold but (3) does not, the mod threw during load. `console.txt`
has the reason.

## 10. Did the sidecar attach?

```
status.bat
```

or, machine-readable:

```
pz-agent status --json
```

Attached means: `heartbeat.sidecar.json` is being written, `session.json` names
a session both sides agree on, and `status` reports the mod as present. A
sidecar that is running but reports the mod absent almost always means the two
sides resolved **different exchange directories** — `doctor` prints both.

## 11. Which mode is it in?

`pz-agent status` reports the mode:

- **OBSERVE** — attached, watching, planning; cannot act. This is the state
  after `start`, and the state after any disarm, takeover or panic stop.
- **ASSISTED** — may act on commands you give it.
- **AUTONOMOUS** — may also act on its own within the configured bounds.

The mode also appears in the snapshot under `safety.mode`, which is the value
the mod itself is acting on. If the CLI and the snapshot disagree, believe the
snapshot and investigate: that disagreement is a real defect, not a display bug.

## 12. Panic stop

Any of these, in order of how fast they are to reach:

1. **Voice:** say «стоп». It fires on the interim transcript, not the final one.
2. **CLI:** `pz-agent disarm` — returns to OBSERVE and ends the in-flight
   action.
3. **In-game:** move the character yourself. Manual takeover is detected and
   ends the in-flight action as `USER_TAKEOVER`.
4. **Last resort:** close the sidecar. Heartbeat loss makes the mod stop and
   disarm on its own — the mod does not keep acting when nothing is supervising
   it.

A panic stop cancels only timed actions the mod owns. Work you queued by hand is
left alone, deliberately: cancelling it would be the agent overriding you at the
exact moment you took control.

## 13. Restoring the test save

```
pz-agent backup-save --list
pz-agent restore-save <backup-id>
```

`restore-save` **refuses while Project Zomboid is running**, and that refusal is
load-bearing: restoring over an open save destroys it. Close the game first.

Backups are hash-manifested, so a restore verifies what it is about to write.
A backup that does not verify is refused rather than restored partially.

Use a dedicated test world. Do not point any of this at your main save.

## 14. What to send when something breaks

```
pz-agent logs --bundle
```

That builds a redacted archive. Send it together with:

- the whole of `%USERPROFILE%\Zomboid\console.txt` — not an excerpt; the
  load-time errors are at the top and an excerpt of the tail loses them;
- `evidence/S<nn>_<NAME>/` for the failing scenario;
- `pz-agent doctor --json`;
- the exact build string from the game's main menu.

Do not edit any of it. The bundle is designed to be safe to share as it stands;
an edited bundle is evidence of nothing.

## 15. What not to rewrite

The following were designed against specific failure modes. Changing them
without a live test that demonstrates a problem will reintroduce a bug that was
already found and fixed.

| Component | Why it is the way it is |
|---|---|
| Reference parsing (`Refs.lua`, `protocol/refs.py`) | Parsed from both ends because a container tail contains colons. A naive left-to-right split yields a **valid reference to a different object** without erroring. |
| The honest-success invariant (`protocol/messages.py`) | A `succeeded` result structurally requires postcondition evidence. Relaxing this makes every downstream PASS a guess. |
| Snapshot slots and the pointer | Atomic rename is not guaranteed in the mod's Lua runtime. Alternating slots with the pointer written last is what replaces it. |
| Ownership tagging (`Ownership.lua`) | Untagged actions are indistinguishable from the player's own, so panic stop would refuse to cancel them — correctly. |
| The dispatcher's argument declaration | Undeclared keys are rejected rather than ignored. A silently dropped `radius` runs the action with a default nobody asked for. |
| `Outcome.NOT_RUN` in the harnesses | A dry run cannot produce a pass; filtering changes what runs, never what is reported. |
| `restore-save`'s `game_running` argument | It has no default, on purpose. It was once passed `False` unconditionally, and that bug would have overwritten a save with the game open. |
| Capability states | A static scan yields `available_unverified` at best. Only a live ack promotes anything to `verified`. |

What you **should** change freely: engine symbol names, constructor signatures,
stat accessors and `required_symbols` entries — anything the real Build 42.20
spells differently. That is what this handoff is for.
