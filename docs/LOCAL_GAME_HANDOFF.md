# Handoff to the local game machine

This document hands the project from the remote environment — where it was
written — to a Windows machine where Project Zomboid Build 42.20 is actually
installed. It says what exists, what was proven, what was **not** proven, and
exactly how to run it.

## 1. Where the work is

| | |
|---|---|
| Repository | `natural0101/poject-zombigpt` |
| Branch | `main` |
| Base | — `feature/playable-agent-1.0` (base `dev`) is merged in, and `main` has moved past it |
| Merged into `main`? | **Yes.** An earlier revision of this row said no, and that merging would wait for live evidence. The merge has since happened — `fef7edd` brought the Windows, MCP and voice runtime work into `main` — and evidence still does not exist: all twenty-two scenarios remain `NOT_RUN`. What still waits for evidence is the tag and the release. See §4. |
| Tag | **None.** `v1.0.0` is deliberately not created. See §4. |
| Release candidate | `pz-agent-windows-v1.0.0-rc1.zip`, built by the `windows package` workflow and uploaded as the `pz-agent-windows-rc` artifact. That build is the artefact of record: it is the only one that contains the two Windows executables, because they are compiled on the Windows runner. A local `build_rc.py` here produces an archive without them, which is useful for checking the layout and is not a release candidate. `docs/control/EVIDENCE_INDEX.md` carries the commit, run and digest of the current one; see §5 |

```
git fetch --all --prune
git checkout main
git pull --ff-only
git rev-parse HEAD
```

Record that commit hash — every `result.json` the live-test runner writes
carries it, and a run whose commit does not match the code under test is not
evidence of anything.

## 2. What is implemented

Everything that can be implemented without a running game.

**Protocol and IPC.** A closed action whitelist of 22 names (17 of them game
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
it: removing the wiring fails fourteen of its fifteen tests, measured by doing
exactly that in a scratch copy of the tree.

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

**A spoken goal now reaches the planner.** An earlier revision of this
paragraph said nothing carried a goal from a second process into the running
sidecar, and that goals were therefore refused. That stopped being true when
the Local Core RPC link landed (`CORE_RPC.md`, in the repository's `docs`
directory — not carried in the release archive): `voice run` submits a
spoken goal to the running sidecar over `goal.submit`, by the same method, the
same codec and the same refusals a tool call uses, so the microphone is not a
privileged caller — the seam is `pz_agent_cli/voice.py`. The channel is
deliberately narrow: a transcript resolves to one of four closed tokens —
`eat`, `drink`, `read`, `resume` (`VoiceGoal` in `pz_agent_voice/messages.py`)
— or to nothing, and no transcript text crosses the boundary. A goal needs a
running sidecar: `voice check` dials `goal.status` before saying where a goal
would go, and names `pz-agent start` when nothing answers. The stop
deliberately does not take the link, for the reason above — a goal needs a
sidecar, and a stop has to work when nothing is running.

**Windows packaging.** Self-contained executables — PyInstaller specs, built
only by the Windows CI, so absent from a ZIP built in this container; see §5 —
the BAT files listed in §6, an installer and uninstaller, and the RC ZIP.

## 3. What was verified, and how

Everything below ran and passed in the remote environment:

- the Python test suite (unit, contract and integration) under **both**
  supported Python versions, in clean editable installs. That claim is now
  **older than the tree**: at the commit this revision was measured against
  (`edeff8e`) the suite measures 6574 passed, 4 skipped and **3 failed**. Two of
  the three are the master-plan gate refusing the recorded plan — a task marked
  PASS depends on one still `NOT_STARTED`, and `STATUS.json` describes an older
  commit — and the third is `tests/unit/test_voice_plan_port.py::
  test_a_goal_the_channel_refuses_is_spoken_about_and_not_echoed`: the
  goal-channel work gave the "busy" refusal its own sentence and the test still
  expects the generic one. The suite is **not green today**, and only
  Python 3.11.15 has pytest installed in this container, so the 3.12 half is CI
  configuration rather than a result observed here. Treat the matrix as
  declared, not demonstrated. This line had been standing
  since long before voice, memory, the live-test runner and the packaging landed
  — it was re-run rather than inherited, because a claim about a build is only
  about the build it was made against;
- `mypy` in strict mode over the whole project — still clean when re-run at the
  current commit;
- `ruff format --check` and `ruff check`. Format is still clean;
  `ruff check` re-run at the current commit finds **2 errors**, both RUF002
  confusable-Cyrillic hits in a docstring the in-flight goal-channel work added
  to `tests/unit/test_voice_session.py` without the `noqa` guard its siblings
  carry;
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
  wrote the same 30 files as installing from a clone (the byte total this line
  once gave, 506 613, has drifted with the mod and is not restated),
  every installed file is byte-for-byte identical to the source tree, and all 29
  Lua files in the archive parse. The archive had been built and checksummed
  several times without anyone installing from it, which is a different claim;
- **the live-test runner's own commands**, as far as they go without a game.
  `live-test status` on a fresh state directory lists all twenty-two scenarios
  as `NOT_RUN`. `live-test prepare` refuses without `--save <mode>/<name>` — there
  is no default, because guessing which world to experiment on is how a main
  save gets used — and exits 1. **An earlier revision of this line said prepare
  "writes nothing when it refuses". That was wrong.** It creates a directory per
  scenario first, every time, and then refuses; what it withholds is
  `prepare.json`, the record that says the tree is ready. Scaffolding, not
  evidence — but "writes nothing" invited you to believe a failed prepare left
  no trace, and it leaves one directory per scenario. `live-test finalize` refuses and
  names every missing artefact, one line each;
- **the support bundle, with a real secret and a real home path planted in the
  logs.** `logs --bundle --verify` struck out an AWS-shaped key, a private-key
  header and an `api_key=` assignment, and rewrote the home directory and the
  account name. Then the archive was unzipped and its bytes read directly,
  rather than trusting the verifier's own "clean" — which is how the false
  positive below was found;
- **`live-test collect` and the MCP server.** `collect` names every file it
  could not find, one line each, and reports how many it copied and how many it
  skipped rather than a bare success — on an untouched workspace that is "copied
  0", and every skipped line is a path you can go and look at;
  `pz-agent-mcp --describe` answers with the whole published surface — 34 tools
  and 7 resources, re-measured at the current commit after the goal channel
  added `pz_goal_submit`, `pz_goal_status` and `pz_goal_cancel` — with no game
  and no sidecar, which is what makes it the thing to run first when writing a
  client;
- **`restore-save`, both directions.** A save was corrupted — one file
  rewritten, one deleted — and restored from a backup taken by `backup-save`;
  both came back. Then, with a live game heartbeat in the exchange directory,
  the same restore was refused with exit 1 and the tampered file left exactly
  as it was. There is no flag that overrides that refusal, and this is the
  command that can destroy a save, so it is the one worth having watched;
- **the sidecar lifecycle, `start` → `status` → `stop`**, which is how the
  defect above was found;
- **the whole loop you will perform, end to end, through the real commands.**
  `backup-save` on a synthetic Zomboid directory, then `prepare --save`, then
  `run` — and the three refusals in between: a save whose name does not say
  "test", a test save with no backup, and an evidence directory with no schemas.
  `run` is refused before prepare and permitted after it, which is the pair that
  matters: a gate whose precondition can never be met is a bricked release, and
  until this was driven nothing here could tell that apart from a gate working;
- **that `run` and `resume` refuse until prepare has completed.** They did not.
  `prepare.json` was written by `prepare` and read by nothing, so the check
  proving you named a test save and hold a backup that reads back produced a
  record nobody consulted, and the deliberately destructive scenarios would
  start regardless. They now refuse and name the command to fix it. `status` and
  `collect` are deliberately not gated: reading the table and gathering logs
  change nothing, and gating them would leave you unable to see why you are
  stuck;
- the Lua↔Python reference agreement, directly, for a world container reference
  carrying five colons — the case a naive left-to-right split resolves to a
  *different object* without erroring.

## 4. What requires a real game — and was therefore not done

**Read this before you spend a session: parts of the sidecar are wired to a mod
that cannot drive them. One of them means the agent cannot loot, and one is a
safety rung that has never fired.** None of this is your install. Reporting it as
a bug costs you the session, which is the only resource in this project that can
produce live evidence at all.

**You can see all of it without the game.** One test builds an observation
through the mod's own readers and reads it back with the sidecar's own decoder:

```
pytest tests/contract/test_observation_document_round_trip.py -v
```

That is worth ten minutes before you start, because this table has been wrong
before. A previous revision of it said the agent could not walk at all and that
every placement would be refused; both were produced by a checker that searched
the sources for a spelling the mod does not use, and both were retracted. The
rows below are the ones that survive a test which runs the mod instead of
reading it.

| What you will see | Why | Do |
| --- | --- | --- |
| **A move that changes storey refuses `PATH_NOT_FOUND`** — a move on one floor is fine | `movement._check_square` wants the `stairs` semantic on the *square* entry; the mod puts it on the staircase *object* standing there, deliberately, so as not to write one fact in two places. The gate is restrictive when the token is missing, so this costs journeys between storeys and nothing else | Walk on one floor. Do **not** relax the precondition — it refuses toward caution |
| **A square behind a closed window is refused as `blocked`, not `closed_window`** | The dedicated `POLICY_DENIED` branch never fires — no such token is produced anywhere — but `Observe.squarePassable` reads the square as impassable, so the refusal still happens under another name | Nothing. Only the *name* of the refusal is wrong |
| **Every container action against a crate, cupboard or corpse refuses `INVALID_REF`** — "is not in the observed container tree" | The mod's inventory has two roots, the main inventory and each worn container, with carried containers nested inside items. A nearby world container is *referenced* — `buildObject` mints it a proper container ref — but never listed, and `resolve_container` searches the list alone | Skip `loot_area`. The mission can now walk to the crate and is refused **at** the crate. This is the one remaining gap that costs a whole goal kind |
| A rotten meal, an empty bottle and a finished book all read as fine | The mod sends `rotten`, `pages`, `amount`/`capacity`; the sidecar reads `freshness`, `pages_total`, `remaining_units`. Different names for the same facts, so every one of them reads as its type's default. `poisonous` (food) and `tainted` (fluid) *do* cross, so the two sharpest hazards are still refused | Do not trust a food or drink *choice* as evidence of anything. Whether it was eaten is still observed honestly; **which** item the policy picked was decided blind |
| A zombie arriving during a meal or a book does not interrupt it until the danger is high enough to stop everything | §17.2's interrupt rung matches `ActionState.type`, and the mod never fills that field | Worth timing if you can: the flee rung above it does fire, so the character should still stop — later than the spec asks. If it does **not** stop at all, that is a finding |
| Containers are never refused as unreachable | `container.accessible` is always true: five sidecar sites refuse on it and nothing in the mod ever sets it false | Nothing. Expect a locked or blocked container to be attempted and to fail at the game rather than be refused early |
| Combat stops at `weapon_unusable` rather than swinging | `weapon_condition_fraction` reads `item.extra["weapon"]`; the mod puts the wear in `player.stats` instead, deliberately, and nothing reads it there. Unreadable makes the policy refuse rather than guess | Nothing — and note `combat_assist` is experimental, so you should not reach this at all |
| Snapshots are always full, never deltas | `Observe.context` sets `full = true` unconditionally | Nothing. The merge path in `store.py` is simply unused |
| A horde one storey up counts as closing | `dangerFloor` tests `zombie.position.z`, and its caller passes the raw reader table where the position is three flat fields | Nothing. The error runs toward caution — the floor reads higher than the world warrants, never lower |

`tests/contract/test_gates_without_producers.py` is the ledger for all of these
and `tests/contract/test_item_domain_vocabularies.py` measures the item blocks;
`LIMITATIONS.md` carries the full account. Both compare the two sides by
*pattern*, which is how one row came to be wrong for four commits — prefer
`test_observation_document_round_trip.py`, which runs the mod.

**So what is worth doing with a live session?** Almost everything, and one thing
above all others:

1. **Confirm the engine symbols in `docs/GAME_API_VERIFICATION.md`** — that
   document states how many, and is the one place that does. This is
   the highest-information hour available, because almost every remaining
   unknown is downstream of it. The square tier is now built and published — the
   round-trip test proves the two sides agree about it — but that agreement says
   nothing about whether `isSolid`, `isSolidTrans`, `isFree` and `getFloor`
   answer at all on this build, and a wall published as open ground is the
   failure that sank an earlier attempt at the tier. **Those four are not yet
   rows in that document.** Add them and confirm them. Start with
   `ISTakeWaterAction`, whose argument order the document flags as wrong-filling
   silently if the build differs.
2. **Arm, disarm, panic stop, manual takeover.** These need no movement, and
   they are the safety guarantees the README makes.
3. **Walk one square, then walk across a room.** This is newly worth doing: the
   square tier exists, so a walk should now be attempted rather than refused at
   the precondition. What a live run settles is whether the squares the mod
   publishes describe the world correctly — the round trip only proves the
   sidecar reads what the mod writes.
4. **The observation tiers** — player, inventory, nearby. Whether `getTarget`,
   `getZombieList` and the body readers exist on this build decides how much of
   the threat assessment is running on measured readings rather than on the
   conservative substitutes this branch installed for their absence. Check the
   `unread` block in the planner's view and the `observe.*` counters in
   `player.stats`: **if any of them fire on a healthy install, that is the
   finding**, not a nuisance.
5. **Eat and drink from the inventory, equip, unequip, bandage, read.** These
   reach the game without a step being taken. Watch the *choice* rather than the
   act: the vocabulary mismatch above means the policy picked that sandwich
   without being able to read rot, portions or pages. If the agent eats
   something visibly spoiled, that is the mismatch showing, not a new bug — and
   it is worth reporting as confirmation that it bites in a real save.
6. **One thing to time, if the session allows.** Start a read or a meal and let
   a zombie approach. §17.2 asks for an interrupt at the lower threshold; that
   rung is dead, so the character should stop only when the danger reaches the
   flee threshold. Confirming *that it stops at all* is the safety-relevant
   half. If it does not, stop the session and say so — that is the one finding
   in this list that would change what the agent is allowed to do.

This is the honest part, and the reason this handoff exists.

**There is no Project Zomboid in the environment this code was written in.** It
is a Linux container: no Windows, no Steam, no Wine, no game. So none of the
following was done, and none of it is claimed:

- **S01–S22 live scenarios.** All twenty-two are `NOT_RUN`. The runner's initial
  state is `NOT_RUN` precisely so that a scenario nobody ran cannot report a
  pass.
- **The 30-minute and 2-hour endurance runs.**
- **Measured p50/p95 latencies.** Any number here would have been invented.
- **Build 42.20 API signatures.** Every engine symbol is *declared* and probed;
  none is confirmed. `docs/GAME_API_VERIFICATION.md` is the list, and states its
  own size; this file deliberately does not restate it. The grep
  `grep -rn "Build 42:" pz-mod/` finds only the handful of symbols carrying that
  comment and was once described here as the whole list, which understated the
  surface by a factor of three.
  **Start with `ISTakeWaterAction`.** Three places in this repository once
  stated three different argument orders for it; `docs/GAME_API_VERIFICATION.md`
  now records the one the mod actually calls —
  `:new(character, waterObject, amount, item)` — and marks it as the first row a
  live run must confirm. A build that orders them differently fills the wrong
  thing and does not error, which is exactly why `drink_world_source` is capped
  at `experimental` and why `consume.drink_source` refuses to treat the vessel's
  own volume as proof of anything.
- **`v1.0.0` and its GitHub Release.** `scripts/check_release.py --release`
  fails today — it refuses `v1.0.0` with 4 of its 8 checks: no
  `release/evidence-manifest.json`, no test report handed to the gate, and the
  RC archive missing the two executables in both the completeness and the
  `bin/` check. That failure is the gate working.

The RC ZIP is built and its SHA-256 is printed by the build. That hash covers
the artefact, not any claim about it having been run in a game.

## 5. Installing

### Getting the ZIP, and checking it is the one

Nothing above says where the archive comes from, so: it is a workflow artifact,
not a release asset. Open the `windows package` run named in
`docs/control/EVIDENCE_INDEX.md` — that file is part of the repository rather
than the archive, so it is named here rather than linked — and download
`pz-agent-windows-rc`.

**GitHub wraps it, and the wrapping matters.** What downloads is
`pz-agent-windows-rc.zip`, and inside it are two files:

```
pz-agent-windows-v1.0.0-rc1.zip
pz-agent-windows-v1.0.0-rc1.zip.sha256
```

The inner ZIP is the release candidate. The outer one is packaging GitHub adds
and takes no part in anything — which is why the SHA-256 the Actions page shows
beside the artifact is **not** the archive's digest. It is the wrapper's. Those
two numbers have never been equal and never will be, and confusing them is the
easiest mistake to make here.

Extract, then check the inner ZIP against the sidecar that travelled with it:

```
certutil -hashfile pz-agent-windows-v1.0.0-rc1.zip SHA256
type pz-agent-windows-v1.0.0-rc1.zip.sha256
```

The first prints a digest; the second prints the same digest followed by the
file name. They must match, and both must match the `archive sha256` row in
`EVIDENCE_INDEX.md`. Three independent statements of one number: the builder
wrote the sidecar, the release gate printed it into the workflow log, and the
evidence index records it against the commit and run it came from. If any two
disagree, stop — you are not holding the archive this project certified.

Then extract the inner ZIP and run `install.bat` from where it unpacked.

### Running the installer

From the RC ZIP (no Python, no git needed — **but only from an RC built on
Windows**; the Windows CI builds `bin\pz-agent.exe` and `bin\pz-agent-mcp.exe`
with PyInstaller, and the ZIP in this container's `dist/` was built without
them. Its own `BUILD-MANIFEST.json` says so — `complete: false`, the two
executables listed as missing — and `install.bat` then falls back to a
`pz-agent` already on `PATH`, which a clean machine does not have):

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

**One thing to check while you are there, and it takes five seconds.** With the
game open, run:

```
tasklist | findstr /i zomboid
```

The refusal has two sources: a live game heartbeat, which is proof, and — when
there is no heartbeat — the process table, which is matched against the literal
`"zomboid"`, case-folded. Nobody has read the process table of a machine with
Build 42.20 open, so whether the real process name contains it is unconfirmed.
Send whatever that command prints, or that it printed nothing.

The direction of a wrong guess is the safe one: an unreadable listing, a
truncated one, and one that matches nothing all yield `MAY_BE_RUNNING`, and the
restore is refused. So a wrong marker costs you a refusal to work around, never
a lost save — which is why this is a question rather than a blocker.

Backups are hash-manifested, so a restore verifies what it is about to write.
A backup that does not verify is refused rather than restored partially.

Use a dedicated test world. Do not point any of this at your main save.

## 14. What to send when something breaks

```
pz-agent logs --bundle
```

That builds a redacted archive containing the sidecar's own logs
(`logs/pz-agent.log` and `logs/pz-agent.jsonl`) and its trace
(`traces/session.jsonl`). Both are written by a session that ran — before this
branch neither existed, which is why the scenarios' log lists and
`live-test collect` had never had anything to copy. If a bundle from a real run
still has no `logs/`, that is a finding worth reporting on its own.

`pz-agent replay <state>\traces\session.jsonl` steps through what the sidecar
saw and did: each observation as a snapshot or a diff, each action beside the
result that closed it. The trace is bounded and rotates, so a long scenario
keeps its recent past rather than the whole run — run `live-test collect` at the
end of each scenario rather than at the end of the day. `collect` takes the
current file and every rotated generation into that scenario's `logs/`, without
being asked: the scenarios' `logs` lists were written when nothing
produced a trace, so none of them names one.

Send the archive together with:

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
