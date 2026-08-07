# Progress

Live status of the task graph in
[`docs/blueprint/task_graph.yaml`](blueprint/task_graph.yaml). This file is the
handover point between work sessions: read it first, update it last.

**Legend** — `done` implementation + tests + docs complete and `scripts/check.sh`
green · `wip` in progress · `todo` not started · `live` blocked on a step that
physically requires a running game.

Last updated: 28 of 30 tasks closed; T029 and T030 are blocked on a live game,
not deferred. 3544 Python tests and 2864 Lua assertions across 26 suites,
mypy strict over 266 files, `scripts/check.sh` green — measured under Python
3.11.15, which is the only interpreter with the suite installed here. CI
declares a 3.11/3.12 matrix; that is configuration, not a result observed in
this container.
See FINAL_IMPLEMENTATION_REPORT.md.

Work beyond the original graph is complete on `feature/playable-agent-1.0`:
the protocol grew from fifteen actions to twenty-two, the mod gained a real
command executor and seventeen game adapters, and the sidecar gained the
adapters, providers, live-test harness and Windows release candidate that go
with them. See [the playable-agent section](#the-playable-agent-branch) below,
and [`LOCAL_GAME_HANDOFF.md`](LOCAL_GAME_HANDOFF.md) for what still needs a
machine with the game on it.

**Nineteen defects that branch found are worth reading before any further work**,
because they are one family and the family is not closed. Every subsystem was
written, tested and green; what nothing tested was whether the subsystems were
*connected*.

| # | The defect | What it cost |
| --- | --- | --- |
| 1 | Adapters published under `name`; the dispatcher reads `action` | Thirteen of sixteen game actions unreachable |
| 2 | The two halves of the wire named arguments differently | Every movement and transfer command refused |
| 3 | `move_near` demanded a reference kind the mod never mints | The action could not be called from a real observation |
| 4 | `build_loop` passed no capability check, so it kept `deny_capability` | The assembled sidecar refused *every* action |
| 5 | `build_loop` passes no planner | Autonomous mode proposes nothing |
| 6 | Nothing mapped a backup to the save id the mod reports | Autonomy asked instead of acting; closed by recording the id at backup time |
| 7 | The memory store was complete and connected to nothing | `reserves_item` always answered False, so §7.9 rested on tag rules alone, and no home point could exist |
| 8 | `pz_agent_voice` was imported by nothing and had no entry point | Russian voice control was complete, tested, and impossible to start |
| 9 | The mod could drink from a sink; the sidecar had no argument for it, and the path it did have ran under the wrong capability | Two faults in one place: a working mod feature unreachable from Python, *and* `drink_world_source` — which §12.4 caps at `experimental` — reachable through `drink_carried`, which a scan verifies |
| 19 | **`TraceWriter` was constructed nowhere, so `pz-agent replay` had nothing to replay** | The same shape as 18, one layer in. `docs/QUICKSTART.md` printed `pz-agent replay <trace>` under "When something goes wrong", `logs --bundle` packed `traces/*.jsonl`, and `replay` was a shipped, parsed, documented command reading a format the product never produced. Closing it needed a seam rather than a call: the engine returns a *result* and never lets go of the command it sent, so `ActionEngine.on_dispatch` was added and the loop pairs the two. Writing the trace then exposed a second fault in the format itself — a rotation could leave an observation *diff* as the first line of the new file, and `replay_observations` refuses a diff with no baseline, so every long run's trace would have read back as unreplayable. Found by a test that rotates for real |
| 18 | **`DiagnosticLog` was constructed nowhere, so `logs/pz-agent.log` could not exist** | Written, tested, rotated, redacted, level-filtered — and never built outside the test suite. Nineteen of the twenty live scenarios name that file among the logs to collect and three name `pz-agent.jsonl`; `LOCAL_DEBUG_MAP.md` sends an operator to it; `pz-agent logs` reads it; the support bundle packs its directory. `live-test collect` reported "copied 0 file(s), skipped 15" — the honest answer, and the line read past twice before anyone asked why the sidecar's own log was among the missing |
| 17 | The support bundle's verifier flagged its own successful redaction | `credential_assignment` matched `api_key=<REDACTED>`, so `logs --bundle --verify` printed "REVIEW BEFORE SHARING" and exited 1 over a line whose secret had been correctly removed. Not a leak — the training of a habit that would let one through, in the one artefact designed to be attached to a public issue |
| 16 | `configs/mcp/README.md` documented one of the two refusals a client meets | It said `pz-agent-mcp` "exits with status 1" for missing services. On a plain install the SDK gate fires first and returns 3, with a message about a missing package rather than a missing sidecar — so a client author would have gone looking for the wrong cause on their first launch |
| 15 | **`pz-agent start` reported success on the strength of `Popen` returning** | A fork succeeding says nothing about whether the program ran. A sidecar that died on its first import left `start` printing "sidecar started as pid N" and exiting 0, `arm` then failing for reasons that named nothing, and `stop` reporting "no such process" — also exiting 0. Found by running the lifecycle, not by reading it |
| 14 | **`live-test run` never consulted the prepare record** | `prepare` verifies a test save is named and a backup *reads back*, then writes `prepare.json`. Nothing read it. The one check standing between twenty deliberately destructive scenarios and a main save produced a record nobody consulted |
| 13 | The release archive omitted the two documents its own shipped documents told the operator to open | `GAME_API_VERIFICATION.md` and `LOCAL_AGENT_PROMPT.md` were not in `DOC_NAMES`. Introduced by fixing defect 12: the correction pointed five documents at a file the archive did not carry. Found only by opening the ZIP | It returns six lines against 52 `requires_live` rows — about an eighth. The sentence was in `LOCAL_AGENT_PROMPT.md`, the file the agent starts from, so it would have checked six places and believed the surface covered |
| 11 | Five Lua adapters declared no capability, with comments saying no probe existed for them | Probes exist for all five. The action gate was never open — the mod enforces by required symbols, the sidecar by the ledger — but the mod's capability document named six capabilities where the system knows twelve, so five were absent from the report a person consults when something is refused |
| 10 | **Multiplayer was documented as refused twice and refused nowhere** | `safety.allow_multiplayer` sat in `_advisories`, whose contract is "Never errors", carrying the sentence "multiplayer is refused at the handshake regardless of this setting". No such refusal existed in `packages/` or `pz-mod/`. The setting was the bypass it claimed not to be |

Every one was found by a test that crosses a seam rather than covering a unit,
and each of those tests now exists: `tests/lua/test_adapter_registry.lua`,
`tests/contract/test_adapter_args_agreement.py`,
`tests/contract/test_capability_evidence_agreement.py`,
`tests/contract/test_mcp_action_coverage.py`,
`tests/contract/test_sidecar_capability_wiring.py`,
`tests/contract/test_sidecar_planner_wiring.py`,
`tests/contract/test_backup_attribution.py`,
`tests/contract/test_sidecar_memory_wiring.py`,
`tests/contract/test_voice_wiring.py` and
`tests/contract/test_multiplayer_refusal.py` and
`tests/contract/test_capability_declaration_agreement.py`,
`tests/contract/test_game_api_inventory.py`,
`tests/contract/test_doctor_codes_documented.py`,
`tests/contract/test_sidecar_writes_its_log.py` and
`tests/contract/test_sidecar_writes_a_replayable_trace.py`.

Number ten is the one to read first, because it is not a wiring defect at all —
it is a documented safety guarantee that was never implemented. It is closed by
two gates: the configuration key is a hard error, and
`ActionEngine._multiplayer_abort` refuses every mutating command unless the mod
positively reported single player, with an absent reading refused exactly as
`true` is. `tests/contract/test_multiplayer_refusal.py` holds it, and both
halves were mutation-checked. Nobody has watched it refuse a real server.

Number nine was closed by splitting the action: `consume.drink_source` is its
own action with its own adapter on both sides, so the capability is checked by
the engine before the adapter is entered rather than inside it. The tests that
hold it are `test_the_two_drink_actions_do_not_share_a_capability` and
`test_the_world_source_adapter_will_not_verify_without_the_source_it_names`, and
`drink_world_source` moved out of that file's excuse list into its exercised
table — where every other probe has always had to be.

Each was mutation-checked rather than trusted: the wiring was removed and the
failures counted. A seam test that would not have failed is not evidence that
the seam holds.

The pattern is worth naming, because it will recur. A unit test written beside
the code it covers cannot fail for the reason these failed: both sides were
correct in isolation and the assumption connecting them was never stated
anywhere a test could read it. **The live run is the next seam of the same
kind**, and it is the only one that cannot be closed from here.

## Status

| Task | Title | Phase | Depends on | Status |
| --- | --- | --- | --- | --- |
| T001 | Initialize repository and quality toolchain | 1 | — | **done** |
| T005 | Define protocol domain models and JSON schemas | 1 | T001 | **done** |
| T002 | Detect Project Zomboid installation and user directory | 0 | T001 | **done** |
| T003 | Build local API compatibility scanner | 0 | T002 | **done** |
| T004 | Implement doctor CLI | 0 | T002, T003 | **done** |
| T006 | Implement Lua mod skeleton and heartbeat | 2 | T003, T005 | **done** |
| T007 | Implement sidecar handshake and locks | 2 | T005, T006 | **done** |
| T008 | Implement command queue and acknowledgements | 2 | T007 | **done** |
| T009 | Implement panic stop and manual takeover | 2 | T006, T008 | **done** |
| T010 | Implement save backup subsystem | 2 | T002 | **done** |
| T011 | Observe player scalar state | 3 | T006, T008 | **done** |
| T012 | Observe nested inventory with stable refs | 3 | T011 | **done** |
| T013 | Observe nearby world and threats | 3 | T011 | **done** |
| T014 | Implement action lifecycle framework | 4 | T008, T011 | **done** |
| T015 | Implement movement adapter | 4 | T013, T014 | **done** |
| T016 | Implement inventory transfer adapter | 4 | T012, T014 | **done** |
| T017 | Implement safe food selection and eat adapter | 4 | T016 | **done** |
| T018 | Implement safe drink selection and drink adapter | 4 | T016 | **done** |
| T019 | Implement literature selection and read adapter | 4 | T016 | **done** |
| T020 | Implement deterministic reflex guard | 6 | T009, T013, T014 | **done** |
| T021 | Implement MCP server | 5 | T011, T014 | **done** |
| T022 | Implement permission and autonomy policy | 6 | T017–T020 | **done** |
| T023 | Implement typed planner and critic | 7 | T021, T022 | **done** |
| T024 | Implement memory store | 7 | T012, T013, T023 | **done** |
| T025 | Implement TeamON voice adapter interface | 8 | T021, T023 | **done** |
| T026 | Implement installer and launcher | 9 | T004, T006, T007, T010 | **done** |
| T027 | Implement diagnostics and support bundle | 9 | T004, T008, T014 | **done** |
| T028 | Build live game smoke harness | 9 | T015–T019 | **done** |
| T029 | Run endurance and recovery tests | 9 | T020, T022, T028 | **live** |
| T030 | Produce release artifact and final report | 9 | T021, T025–T027, T029 | **live** |

## Completed in detail

### T001 — repository and quality toolchain

Package layout under `packages/`, `pyproject.toml` with a dependency-free core,
ruff (format + lint, security and stub rules on), mypy in strict mode, pytest
with contract/integration/game-smoke markers, `.luacheckrc` with the engine
globals enumerated, and a GitHub Actions matrix over Python 3.11/3.12 plus a
Lua job.

Three gates beyond the usual linting, all runnable standalone and all wired
into `scripts/check.sh`:

- `scripts/check_forbidden.py` — AST-level scan for stub bodies, `TODO` markers
  in shipped code, `eval`/`exec`/`shell=True`/`loadstring`, plus a secret
  scanner over every tracked text file.
- `scripts/check_versions.py` — the five versions must agree across
  `version.py`, `pyproject.toml`, `mod.info`, the schema consts and the
  changelog.
- `scripts/check_schemas.py` — every schema must itself compile as Draft 2020-12.

### T005 — protocol domain models and schemas

`pz_agent_core.protocol` holds the shared vocabulary:

- `enums.py` — closed vocabularies mirrored by the schemas: action names,
  ack statuses with a terminal set, session modes, danger levels with an
  ordering, container kinds, capability states, risk classes, and the interrupt
  priority ladder from the master prompt.
- `refs.py` — session-scoped references with generation tracking. Parsing is
  done from both ends because a container reference itself contains colons; a
  naive split would corrupt world refs. `belongs_to_session` is what turns a
  reference from a previous session into `INVALID_REF` rather than a silent
  mis-resolve.
- `messages.py` — strict, total parsers for `Command`, `ActionResult` and
  `Observation`. Optional fields are omitted rather than emitted as null,
  because the observation schema is `additionalProperties: false` throughout.

The central safety invariant is encoded here: constructing an `ActionResult`
with `status = succeeded` and any reason other than `POSTCONDITION_MET` raises,
and `ActionResult.succeeded()` refuses to build without evidence. "Queued" can
therefore not be reported as "done" by construction, not merely by convention.

### T002 — installation and user-directory discovery

Walks every Steam library from `libraryfolders.vdf` rather than assuming the
default one; a second library on another drive is the common case. The VDF
parser accepts the modern `path` key and the legacy numeric form, comments and
CRLF, because that file is written by many Steam versions over the years.

Every entry point takes an injectable filesystem root and environment mapping
instead of reading `os.environ` at call time. That is what makes a Windows-only
code path testable on Linux CI, and it is why the tests genuinely cover a
Cyrillic username and a relocated home directory instead of assuming them away.

Build detection reports what it read and where. When the metadata is absent it
says so rather than falling back to `42.20` — a wrong build assumption silently
invalidates every capability probe downstream, which is worse than an honest
unknown.

### T010 — save backup and restore

Manifest with per-file sha256, sizes, total bytes and the source directory with
the user's home redacted to a placeholder. Restore verifies every hash before
writing anything and stages through a temp directory, so an abort cannot leave
a half-save.

`restore` refuses while the game is running — an exception, not a warning, with
no override flag. `prune` is the only deletion path and never removes the
newest backup. A source directory over the configured size cap is refused with
a clear error rather than filling the disk.

### T007 + T008 — session, IPC journal, queue

`ipc/layout.py` exposes the fixed filenames as properties plus a predicate
asserting nothing writes outside them; filenames are constants on both sides,
so a command can never name a file.

`journal.py` appends one record per line and reads by byte offset. A trailing
line without a newline is ignored and re-read next tick rather than parsed
half-written; a complete but unparseable line is reported and skipped so one
bad record cannot stall the stream. Rotation is signalled, not silent.

`snapshot.py` uses alternating a/b slots with the pointer written last, because
atomic rename is not guaranteed from inside Kahlua. A torn read falls back to
the other slot, so the worst case is one stale snapshot rather than a
half-parsed one.

`queue.py` tracks sequences with gap detection, enforces leases at both check
points, and caches only terminal results, bounded by entry count.

The session nonce rule carries real weight: `session.json` left by a crashed
sidecar is perfectly well-formed and would otherwise read as a fresh request to
attach on the next save load. Requiring a nonce different from the previously
accepted session distinguishes "a sidecar is asking to connect" from "a sidecar
asked once and nothing cleaned up".

### T006 + T009 — Lua mod

`shared/PZAgent/` holds the pure logic, tested under a plain interpreter with
no engine present: `Json` (deterministic key order, correct control-character
escaping, no `loadstring` anywhere — decoding is a hand-written scanner),
`Refs`, `Protocol` (version constants and the closed action whitelist checked
before dispatch), `Sequence`, `Ownership`.

`client/PZAgent/` holds the engine-coupled half: `Ipc`, `Heartbeat`, `Session`,
`Safety`, `Hud`, `Runtime`. `PZAgent_Main.lua` wires them and holds no logic of
its own.

Cross-verified directly rather than assumed: an item reference into a world
container — which carries five colons of its own — parses to the same container
tail, runtime id and generation on the Lua and Python sides, and rebuilds to
the identical string. A parser splitting left-to-right would not error here; it
would resolve to a *different container*, which is the whole reason both
implementations parse from the ends.

Lua builders return `nil, reason` rather than raising, following the language's
convention. Every call site must therefore check, and the client modules that
consume them are new — this is the most likely place for a swallowed failure to
hide, and is worth attention in review.

## What `wip` and `todo` mean for each open task

Recording which half is missing matters: a task reading as done when only one
side exists hides the gap rather than closing it.

| Task | Built | Missing |
| --- | --- | --- |
| T029 endurance | `S99_endurance.yaml` and every bound it asserts | The run itself, which needs a live game |
| T030 release | The artefact, its checksums and `FINAL_IMPLEMENTATION_REPORT.md` | The smoke and endurance evidence T029 would produce |

Seventeen game action adapters are registered on both sides:
`movement.move_to`, `movement.move_near`, `world.inspect`, `container.inspect`,
`container.open_nearby`, `inventory.search`, `inventory.transfer`,
`inventory.ensure_main`, `consume.eat`, `consume.drink`, `consume.drink_source`, `literature.read`,
`equipment.equip`, `equipment.unequip`, `medical.bandage`, `survival.rest` and
`survival.sleep`. The control plane is **five** — `session.arm`,
`session.disarm`, `safety.stop`, `action.wait` and `plan.cancel` — and is served
by `PZAgent.ActionRuntime` itself, so a stop can never be queued behind the
thing it is stopping. `plan.cancel` was listed above as an eighteenth adapter
for a while; it is a Python builtin with no Lua adapter, and putting it on the
adapter side made the count agree with the membership only by accident.

`tests/lua/test_adapter_registry.lua` asserts that every one of them reaches
the dispatcher through the real install path, and that the registry holds
exactly the protocol's actions and nothing else. That test exists because the
count above was true of the source and false of the running mod: the adapters
were published under a key the dispatcher does not read.

The CLI exposes seventeen commands: `arm`, `backup-save`, `disarm`, `doctor`,
`install-mod`, `live-test`, `logs`, `remember`, `replay`, `restore-save`,
`smoke`, `start`, `status`, `stop`, `uninstall-mod`, `validate-config` and
`voice`. The `live-test` group carries `prepare`, `run`, `status`, `resume`,
`collect` and `finalize`. This sentence listed fourteen and omitted `remember`,
`voice` and `smoke` — the same rot the paragraph below apologises for, one
revision later. It is derived from `pz_agent_cli.app.COMMANDS`, which
`tests/contract/test_cli_docs_agreement.py` now pins to the parser in both
directions. An earlier revision of this file said
`start`/`stop`/`arm`/`disarm` were deliberately absent because the sidecar loop
was not written. The loop is written and they are in the parser; the note was
left standing after the code moved past it.

## Deviations found by verification

Recorded here rather than quietly closed, because each is a place the
implementation and the blueprint differ for a reason.

**Reference generation is session-scoped, not save-scoped.** Blueprint §3.7 says
the generation in a reference increments after a save/load transition, so a
reference minted before it fails validation. The mod emits `generation = 0`
throughout and never increments it; in-session save/load invalidation instead
works by the sidecar noticing `game.save_id` changed, raising `SAVE_CHANGED`,
and closing the session.

That is coarser and stronger: ending the session invalidates every reference
*and* closes in-flight commands as `lost`, where a generation bump alone would
leave a command mid-flight against refs that had just become meaningless. The
counter was left at zero rather than wired to `Session.generation`, which
increments per handshake — a different quantity, and threading it in would put
a number in the field that does not track what the field claims to.

Revisit if a save/load transition is ever made survivable without a new
session; until then the coarse invalidation is the safer reading.

**`Refs.KIND.OBJECT` has no builder on either side.** Two non-container world
objects standing on the same square therefore share one `square:` reference.
Harmless for diffing — `diff.py` degrades to a whole-list diff when references
repeat — but it turned out not to be harmless everywhere.

`movement.move_near` required exactly that kind. `PZAgent.ObserveModel` mints a
`container:` reference for a nearby thing that holds a container and a `square:`
reference for everything else, so the adapter refused every reference the mod is
capable of producing, and the action could not be reached from a real
observation at all. Both the adapter and the plan parser now accept
`container`, `square` or `item`, which is what the mod actually emits.

The note above had been sitting in this file, correct and unread, while the
defect it describes was live. A deviation recorded is not a deviation handled.

**Build 42.20 accessor names are unverified.** The mod's probes degrade to an
absent field rather than guessing, so the behaviour is honest, but which
accessors actually exist needs a live session.

## Known gaps and caveats

- **Both entries that used to stand here are out of date and are recorded as
  such rather than deleted.** One said T003 was unfinished and that any action
  adapter had to wait for it; the scanner has since closed and seventeen
  adapters are registered. The other said T028 was partial because the scenario
  definitions existed without a runner; `pz-agent live-test` now drives twenty
  of them and refuses to finalize without evidence. Both notes outlived the work
  they described, which is the failure mode this section is most prone to — a
  caveat nobody deletes reads as a caveat that still applies.
- **`tests/lua/` proves logic, not compatibility.** It runs under mocked engine
  globals. It cannot demonstrate that `ISInventoryTransferAction` or
  `ISEatFoodAction` behave as expected in Build 42.20, and nothing in this repo
  claims otherwise.

## Requires a live game session

No scenario has been run — there is no installed game in this environment. The
sixteen definitions in `tests/game-smoke/` name what closes each one.

Two non-scenario items also need a real installation:

- **Which file Build 42.20 ships its version in.** Only the `versionNumber=`
  header in `console.txt` is confirmed. The install-side candidates
  (`version.txt`, `version`, `media/version.txt`) are unverified guesses. The
  behaviour is honest either way — an unreadable or absent file reports
  `known=False` with the reason recorded, never a substituted `TARGET_BUILD` —
  but which path actually exists is unknown until someone runs `pz-agent
  doctor` against the game.
- **A real "is the game running" probe.** `BackupManager.restore` requires
  `game_running` as a keyword with no default and no override, so the rule
  cannot be bypassed by accident. Nothing yet supplies it from an actual
  process check; whoever wires the CLI must, and a wrong answer here is the one
  that corrupts a save.

| Scenario | Status | Blocked on |
| --- | --- | --- |
| S01 heartbeat | not run | a live session |
| S02 panic stop | not run | a live session |
| S03–S08 actions | not run | T015–T019 adapters, then a live session |
| S09 manual takeover | not run | a live session |
| S10 stale sidecar | not run | a live session |
| S11 invalid ref | not run | a live session |
| S12 path blocked | not run | T015, then a live session |
| S13 zombie interruption | not run | T020, then a live session |
| S14 backup / restore | not run | a live session (the code and its tests exist) |
| S15 restart recovery | not run | a live session |
| S99 endurance | not run | everything above |

"Not run" is the honest status and stays until an evidence artefact exists.

## The playable-agent branch

`feature/playable-agent-1.0` takes the build from "every subsystem exists" to
"the mod can execute a command and prove what it did". It is not merged, and it
must not be merged before the live evidence exists.

### The protocol grew

Fifteen action names became twenty-two, and the mod's adapter files own
seventeen of them (the other five are the control plane the runtime serves
itself). Six were missing outright — `container.inspect`, `container.open_nearby`,
`inventory.search`, `medical.bandage`, `survival.rest`, `survival.sleep` — and
two were renamed rather than aliased: `inventory.equip`/`inventory.unequip` are
`equipment.equip`/`equipment.unequip`, because the dispatcher's whitelist decides
what may reach an adapter at all and two keys for one action is a second door.

`PROTOCOL_VERSION` is `1.1`. `SCHEMA_VERSION` stays `1.0`: the document shapes
did not change, only an enum inside them gained members, and a schema bump would
have invalidated every stored plan and observation for a change they read fine.

### The mod can now execute

`CommandReader` → `CommandDispatcher` → `ActionRuntime` → an adapter, with an
acknowledgement written at every transition. One command in flight, one waiting,
lease checked before each step, TTL, idempotent replay, session validation,
panic stop, manual takeover and heartbeat-loss stop.

`ActionRuntime` holds the invariant everything else rests on: there is a single
constructor for a success ack, it requires a non-empty evidence table, and an
adapter that finishes with nothing to show yields `POSTCONDITION_FAILED`.

### What running it found that reading it had not

Four defects, each caught by executing the code rather than reviewing it:

- **Thirteen of sixteen game actions were unreachable.** The adapters were
  written and individually tested; they named themselves under `name` while the
  runtime looks up `adapter.action`, so they registered nowhere. Every adapter
  test passed against code wired to nothing. `tests/lua/test_adapter_registry.lua`
  is the question none of them asked, and it went red immediately.
- **Arguments were silently dropped.** The dispatcher builds the argument table
  it hands an adapter *from the adapter's declaration*. An adapter that declared
  nothing was not refused — it ran with every argument gone. Declarations are now
  mandatory, asserted at load, and carry real bounds.
- **`RUNTIME_OWNED` was referenced and never defined**, in the branch deciding
  whether a published adapter supersedes a built-in one. `install` raised on any
  build where `adapters/` had published anything.
- **A lease expiring mid-flight reported `ACTION_TIMEOUT`**, telling the sidecar
  its adapter was slow when its own grant had lapsed.

### Status of the new work

| Block | Status |
| --- | --- |
| Protocol extension to 22 actions | **done** |
| Lua command executor and capability runtime | **done** |
| Seventeen Lua game adapters | **done** |
| Adapter-registry integration test | **done** |
| Python adapters for the new actions | **done** |
| Medical triage policy | **done** |
| `openai_compatible` and `teamon` plan providers | **done** |
| Live-test runner and evidence structure | **done** — its own commands were run before handover |
| Windows release candidate and CI | **done** — RC built; the two `.exe` files need a Windows PyInstaller run |
| `consume.drink_source`, and the capability gate under it | **done** |
| Handoff documentation | **done** |
| S01–S20 live scenarios | **live** |
| `v1.0.0` tag and release | **live** |

### Handoff documents

Written for the machine that has the game, because that is the only place the
remaining work can happen:

- [`LOCAL_GAME_HANDOFF.md`](LOCAL_GAME_HANDOFF.md) — what exists, what was
  verified, what was not, exact paths, and what not to rewrite.
- [`LOCAL_DEBUG_MAP.md`](LOCAL_DEBUG_MAP.md) — symptom → module → log → action.
- [`GAME_API_VERIFICATION.md`](GAME_API_VERIFICATION.md) — every engine symbol
  the mod assumes, all of them `requires_live`.
- [`LOCAL_AGENT_PROMPT.md`](LOCAL_AGENT_PROMPT.md) — the prompt itself.

## Deviations from the blueprint

| Blueprint | Here | Why |
| --- | --- | --- |
| Python 3.12+ | `requires-python = ">=3.11"` | The build environment runs 3.11; CI tests both 3.11 and 3.12 so the 3.12 target stays honest. No 3.12-only syntax is used. |
| A single `setup` command (blueprint §14.2) that detects, backs up an existing same-id mod, installs, creates config, runs doctor and prints launch steps | `install-mod`, with `validate-config` and `doctor` as separate steps that `docs/QUICKSTART.md` sequences | The composition is a preference; **the backup step is a deliberate refusal**. `install-mod` audits the destination before writing anything and raises `ForeignFileError` on the first file pz-agent did not install, or on any installed file whose hash has changed. Backing up and overwriting would still have overwritten; refusing and naming the file does not. Nothing is written when the audit fails. |
| A `support-bundle` command (blueprint §14.7) | `logs --bundle`, with `--verify` | Same subsystem, reached through the command a user is already in when they need it. `docs/TROUBLESHOOTING.md` gives the real invocation. |
