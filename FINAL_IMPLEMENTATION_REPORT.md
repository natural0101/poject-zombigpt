# Final implementation report — pz-agent

Prepared to the gate in [`docs/RELEASE.md`](docs/RELEASE.md), whose "The final
report" section lists nine things this document must state. They are §1 to §9
below, in that order.

**Base commit:** `c4320f0`, which `main`, `dev` and
`claude/workflows-routines-docs-l294qp` all point at — the three branches were
brought onto one commit here, with no divergence between them. `c4320f0` is a
control-plane-only commit over `f397d21` (it touches `docs/control/STATUS.json`
and `docs/control/EVIDENCE_INDEX.md` and nothing else), so `f397d21` is the code
tree every CI verdict below names.
**Versions:** product 0.1.0 · protocol 1.1 · rpc protocol 1.0 · schema 1.0 · mod 0.1.0 · supported build 42.20

A report cannot name the commit that contains it — the hash does not exist until
the commit is made. The hash above is this report's parent. Check
`git log c4320f0..HEAD` before trusting any number here against a newer tree.

**Note the version.** The release candidate is named `v1.0.0-rc1`, and every
version constant in the tree says `0.1.0`. No `1.0.0` exists in `version.py`,
`pyproject.toml` or either `mod.info`, and no git tag exists at all. The RC
filename is a target, not a state.

Every figure below was produced by running something at this commit:
`bash scripts/check.sh` (exit 0, `All checks passed`),
`.venv/bin/python -m pytest --collect-only` (**8586 collected across 230
files**), the Lua harness (**4821 assertions across 32 suites, 0 failed**, summed
from its own per-suite lines), `.venv/bin/python scripts/master_report.py` (the
weighted figures in §1), and CI's own release gate for §8 — not a local
re-derivation of it.

The suite has grown from the 6737 an earlier revision measured — the crafting
and building wave, then the observation-seam round trip, then the checks that
put the plan's own command lines through the CLI parser — and CI's gate agrees
with the collector from the other side: it reports **8537 of 8586 passed, 49
skipped, no failures and no errors**, which is the same 8586 counted by a
different program on a different operating system. Numbers are re-measured at
each revision and never carried over; that is why this paragraph can say the
two agree rather than assuming it.

---

## The short version

The plan of record is [`docs/control/MASTER_PLAN.yaml`](docs/control/MASTER_PLAN.yaml):
484 weighted tasks, measured at this commit by `scripts/master_report.py` at
**73.3% (2305 of 3144 weight)** — 400 tasks `PASS`, 84 `NOT_STARTED`, zero
`FAIL`, zero `BLOCKED`. Five bands are complete: CODE IMPLEMENTATION,
WINDOWS COMPATIBILITY, MCP OPERABILITY, VOICE OPERABILITY and RC PACKAGING
each read **100.0**. The two at **0.0** — LIVE GAME VALIDATION (639 weight)
and FINAL RELEASE (200 weight) — **cannot move in this environment, because
there is no Project Zomboid installation here.** They are not deferred for
convenience; they are physically blocked, and §9 is the complete list of what
closing them requires. The original T001–T030 graph stays as history in
`docs/PROGRESS.md`: 28 of 30 closed, T029 and T030 blocked on the same game.

Nothing in this repository claims to have been verified against the engine. Not
one engine symbol has been confirmed against a running game. All twenty-two
live-test scenarios are `NOT_RUN`, and the runner's initial state is `NOT_RUN`
precisely so a scenario nobody ran cannot report a pass.

Three narrower gaps are known from reading both sides of the seam rather than
from a live run: a floor-changing move always refuses, a square behind a closed
window is refused under the wrong name, and nothing loots a world container.
§9 6a–6c states each with its mechanism. All three are refusals rather than
crashes, and only the third costs a whole goal kind.

---

## 1. What is implemented, by task id

The plan of record, measured at this commit by `scripts/master_report.py`:

| Band | Weight | Progress |
| --- | --- | --- |
| CODE IMPLEMENTATION | 1213 | **100.0** |
| WINDOWS COMPATIBILITY | 290 | **100.0** |
| MCP OPERABILITY | 264 | **100.0** |
| VOICE OPERABILITY | 324 | **100.0** |
| RC PACKAGING | 202 | **100.0** |
| LIVE GAME VALIDATION | 639 | **0.0 — needs the game** |
| FINAL RELEASE | 200 | **0.0 — gated on the band above** |

484 tasks: 400 `PASS`, 84 `NOT_STARTED`, 0 `FAIL`, 0 `BLOCKED`. Of the plan's
54 integration checks, 48 pass; the six open are E14's and E15's — the
live-game statements only a machine with the game can establish. The historical
graph, kept in `docs/PROGRESS.md`:

| Task | Title | Status |
| --- | --- | --- |
| T001–T028 | The full graph through the live smoke harness | **done** |
| T029 | Endurance run | **blocked on a live game** |
| T030 | Release artefact and evidence | **blocked on T029** |

Measured at this commit (each count re-derived by importing the shipped
modules, not read from a document):

| Surface | Count |
| --- | --- |
| `ActionName` members | 22 (17 owned by the mod's adapter files, 5 control plane) |
| Adapters in the assembled registry | 19 |
| MCP tools | 34, of which 19 submit an action |
| MCP resources | 7, none subscribable |
| CLI commands | 17 |
| Capability probes | 12 |
| Live-test scenarios | 20 |

### What changed in behaviour since the last revision

Substantive changes, not bookkeeping — each with its witnessing test:

- **Remote actions are served over the Core RPC link.** The loop owns a
  bounded `ActionChannel` drained at most one submission per tick, through the
  same funnel a planner proposal takes — arming gate, action budget, the
  engine's own capability and permission machinery — so every dispatch still
  happens on the tick thread. Disarm, panic and shutdown end waiting
  submissions with terminal records naming the lever. Remote **plans remain a
  reasoned refusal**, recorded on the port itself: a plan is many engine calls
  driven to their ends in one request, and the tick thread cannot hold them
  without putting its own stop levers out of reach.
  (`tests/contract/test_remote_actions_served.py`, `tests/unit/test_action_channel.py`.)
- **The `action.wait` and targeted `plan.cancel` wires were broken, and the
  contract test's blind spot that hid them is closed.** The sidecar sent
  `game_seconds` where the mod demanded `duration_ms` — a different unit
  against a different clock — and the targeted cancel's `command_id` was a key
  the mod's adapter never declared. The agreement suite had built its registry
  from the game adapters alone and dumped only the mod's published adapter
  list, so the control adapters lived exactly in its gap. It now builds the
  registry the way `pz_agent_cli.app` builds it, dumps both adapter families,
  and closes with a **two-way census with zero exempt actions**
  (`tests/contract/test_adapter_args_agreement.py`).
- **Two movement defects fixed.** Every real walk died on its first poll with
  `INTERNAL_ERROR`: `pollWalk` answered the string `running`, which the two
  movement adapters — keeping their own shape instead of going through
  `Toolkit.declare` — handed to the runtime untranslated
  (`tests/lua/test_movement_runtime.lua`, verified red on the pre-fix code).
  And a move already satisfied answered `POSTCONDITION_FAILED`, because its
  three world readings were necessarily identical; it now returns the full
  outcome table stating the truth — already within radius, nothing queued.
- **Adversarial suites across both remote surfaces.** RPC: hostile frames,
  replay, stale and wrong-server descriptors, death mid-call, restart with
  rotation, partial frames (`tests/unit/test_rpc_adversarial.py`,
  `test_rpc_recovery.py` — servers outlive every barrage, refusals typed and
  bounded). MCP: six hostile paths, each against a live child process
  (`tests/contract/test_mcp_adversarial_e2e.py`).
- **`docs/GAME_API_VERIFICATION.md` was rebuilt against the code**: a sweep of
  195 symbols across every `requires` list, constructor call, accessor string
  and `Events.*` registration; zero missing rows; the five rows that disagreed
  with their call sites (the queue reader, `onSleep`'s argument order,
  `ISReadABook`'s arity, `getBodyParts`, the `PlayerStats` spellings) now
  match them exactly. Every row is still `requires_live` — see §2.

### Thirty-two defects, found and closed

Every subsystem in this build was written, tested and green. What nothing tested
was whether the subsystems were *connected*, or whether the documents describing
them were true. Thirty-two defects of those two shapes were found, each by a test
that crosses a seam rather than covering a unit, and each mutation-checked:

**Nine were wiring** — a subsystem complete and connected to nothing:

1. Adapters published under `name`; the dispatcher reads `action` — thirteen of
   sixteen game actions unreachable.
2. The two halves of the wire named arguments differently — every movement and
   transfer command refused.
3. `move_near` demanded a reference kind the mod never mints.
4. `build_loop` passed no capability check — the assembled sidecar refused
   *every* action.
5. `build_loop` passed no planner — autonomous mode proposed nothing.
6. Nothing mapped a backup to the save id the mod reports.
7. The memory store was complete and connected to nothing.
8. `pz_agent_voice` was imported by nothing and had no entry point.
9. The mod could drink from a world source; the sidecar had no argument for it,
   and the path it did have ran under the wrong capability.

**Eight were documents asserting things the code did not do**, which is the
harder shape, because a reader has no reason to doubt them:

10. **Multiplayer was refused nowhere.** `docs/LIMITATIONS.md` said it was
    "refused in configuration and again at the session handshake" and that "the
    refusal has no workaround setting". No such refusal existed in `packages/`
    or `pz-mod/`. `safety.allow_multiplayer` lived in `config._advisories`,
    whose contract is *"Never errors"*, carrying the sentence "multiplayer is
    refused at the handshake regardless of this setting" — so the flag loaded,
    the agent ran, and the only thing between it and someone else's server was
    a line of advice describing a gate nobody had written. See §5.
11. **Five Lua adapters declared no capability**, each with a comment saying no
    probe existed for it. Probes exist for all five. No command was ungated —
    both sides enforce independently — but the mod's published capability
    document named six capabilities where the system knows twelve, so five were
    absent from the report a person reads when an action is refused.
    `survival_sleep` is the costly one: its `experimental` ceiling exists
    because a sleeping character cannot be reached by a panic stop.
12. **`grep -rn "Build 42:" pz-mod/` was described as listing every guess**, in
    five documents including `docs/LOCAL_AGENT_PROMPT.md`, the file the local
    agent starts from, where it read "Это исчерпывающий список". It returns six
    lines in two files — today against the rebuilt document's 120
    `requires_live` rows, about a twentieth. An agent following that would have
    checked six symbols and concluded the unconfirmed surface was covered.
13. **The release archive omitted two documents its own shipped documents told
    the operator to open** — `GAME_API_VERIFICATION.md` and
    `LOCAL_AGENT_PROMPT.md`. Introduced by fixing number 12: the correction
    pointed five documents at a file `DOC_NAMES` did not carry. Found only by
    opening the ZIP for an unrelated reason.
14. **`live-test run` never consulted the prepare record.** `prepare` proves the
    world is safe to experiment on — a save whose name marks it a test world,
    and a backup that *reads back* — and writes `prepare.json` when both hold.
    Nothing read it. So twenty scenarios that deliberately hurt the character
    and end in restores would start against any save at all, and the only thing
    between them and somebody's main world was a check whose answer went
    nowhere. `run` and `resume` refuse now; `status` and `collect` deliberately
    do not, because reading the table changes nothing.
15. **`pz-agent start` reported success on the strength of `Popen` returning.**
    A fork succeeding says nothing about whether the program ran. A sidecar that
    died on its first import left `start` printing "sidecar started as pid N"
    and exiting 0, `arm` then failing for reasons that named nothing, and `stop`
    reporting "no such process" — also exiting 0. Every supervisor test injects
    a fake spawner, so nothing exercised the one line that mattered.
16. **`configs/mcp/README.md` documented one of the two refusals a client
    meets.** It said `pz-agent-mcp` exits with status 1 when no core services
    are attached. On a plain install the SDK gate fires first and returns 3,
    with a message about a missing optional extra rather than a missing
    sidecar — so a client author's first launch would have sent them after the
    wrong cause.
17. **The support bundle's verifier flagged its own successful redaction.**
    `credential_assignment` matched `api_key=<REDACTED>`, so
    `logs --bundle --verify` — the command `docs/TROUBLESHOOTING.md` tells a
    user to run before attaching an archive to a public issue — printed
    "REVIEW BEFORE SHARING" and exited 1 over a line whose secret had been
    correctly struck out. Nothing leaked. The harm is the habit: a verifier
    that flags its own success teaches an operator to ignore the next flag.

**The remaining fifteen stopped splitting cleanly**, which is itself the
finding. Four are documents naming a command, a route or an enforcement that
does not exist (20–22, 29); two are a field or a variable read in one place and
written in none, or written in one place and read nowhere (23, 28); and nine are
both at once — a subsystem connected to nothing with documents, shipped
archives, live scenarios or a governing agreement already resting on it (18, 19,
24, 25, 26, 27, 30, 31, 32). The family is not closed, and these are no longer
the most recently found: the ones after them are numbered R-001 to R-009 in
`docs/control/BLOCKERS.md`, found by the release-candidate work and recorded
where that work is tracked rather than appended here. All nine R-blockers are
**CLOSED** at this commit; what remains open there is the live family — L-001
to L-003 and RB-002, the no-tag rule — every one blocked on the game.

18. **`DiagnosticLog` was constructed nowhere, so the sidecar wrote no log.**
    Complete, rotating, redacting, level-filtered, well tested — and built only
    by tests, so `logs/pz-agent.log` and `logs/pz-agent.jsonl` did not exist.
    Nineteen of the twenty live scenarios name the first among the files to
    collect and three name the second; `docs/LOCAL_DEBUG_MAP.md` sends an
    operator to it; `pz-agent logs` reads it; the support bundle packs its
    directory. `live-test collect` had been printing "copied 0 file(s), skipped
    15" and naming each absent file — the honest answer to a question nobody
    had asked. `start --foreground` now records the attach, the run's end,
    every retained safety event and the shutdown, at the run's edges rather
    than in the tick, with every write optional and guarded.
19. **`TraceWriter` had the same defect, and `pz-agent replay` had nothing to
    read.** `docs/QUICKSTART.md` printed `pz-agent replay <trace>` under "When
    something goes wrong"; `logs --bundle` packed `traces/*.jsonl`; `replay`
    was parsed, documented and shipped. Nothing wrote a trace. Closing it took
    a seam rather than a call — the engine returns a result and never let go of
    the command it sent, so `ActionEngine` gained an optional `on_dispatch`
    observer and the loop pairs the two. Writing the first trace then exposed a
    fault in the format: a rotation falling on an observation *diff* put one at
    the top of the new file, and `replay_observations` refuses a diff with no
    baseline — so every run long enough to rotate would have produced a trace
    that read back as a refusal. Found by a test that rotates for real.

20. **`SECURITY.md` told a vulnerability reporter to redact with a flag that
    does not exist.** "Do not attach a raw support bundle to a public issue
    until you have checked it with `pz-agent logs --redact --verify`" — there is
    no `--redact`, and the command exits 2 with an argparse usage message. That
    sentence is the single gate between a reporter and an unredacted archive on
    a public issue.
21. **`PRIVACY.md` documented data deletion under a command the CLI does not
    have.** `pz-agent memory --forget`. There is no `memory` command; the real
    one is `remember forget`, and it appeared in no document at all.
22. **`docs/TROUBLESHOOTING.md` sent a user to `pz-agent status --explain`** for
    the food policy's rejection list, and said the thresholds are "in
    configuration" when `[safety]` holds five keys and none of them is one.
    Found by the guard written for 20 and 21 rather than by review, which is the
    point of writing a guard instead of two corrections.
23. **The mod could never publish `experimental`.** `CapabilityRuntime` reads
    `adapter.experimental`; `Toolkit.declare` never carried the field. Read in
    one place, written in none — the same shape as number 1. Two adapters
    carried comments saying "the probe caps this at experimental" and both
    published as ordinary unverified, while `docs/PROTOCOL.md` documents the
    file with an example showing a state its own writer could not emit.
24. **No configuration could produce `disabled_by_policy`.** The state existed,
    the mod guarded on it, `PermissionEngine` refused on it with a message
    written for a user, and `docs/COMPATIBILITY.md` — in the Windows archive —
    listed it as "available, but configuration forbids it" three rows above its
    own warning that a panic stop cannot reach a sleeping character. There was
    no key to write and unknown keys are hard errors. Implemented rather than
    documented away, the way number 10 was.
25. **`game.install_dir` and `game.user_dir` were read by nothing.** The
    documented escape hatch for the two failures that brick every other
    command — a GOG or manual copy Steam does not list, a profile moved by
    OneDrive or `-cachedir`. `doctor`'s own remediation, `TROUBLESHOOTING.md`
    for two codes, and `configs/mcp/README.md` all send a blocked user to set
    them, and setting them did nothing: "configuration is valid", then the
    identical failure telling them to do what they had just done.
26. **`safety.panic_hotkey` had a validator, an error message and no consumer.**
    The mod binds scancode 88 directly and reads no configuration, so any other
    value bound nothing — and this is the stop button. Any value but `F12` is a
    hard error now. Rebinding for real needs the mod to read a published keycode
    *and* a live run to prove the new key reaches the stop; neither exists, and
    saying so is the honest answer.

27. **Seven links in the archive's own README resolved to nothing** — defect
    13's general case, left open by fixing its two instances. An operator on
    Windows has no repository, so a relative link is a file beside the one they
    are reading or nothing at all.
28. **`pz-agent start` printed an MCP configuration setting
    `PZ_AGENT_STATE_DIR`**, a name occurring exactly once in the repository: in
    the literal that printed it. `configs/mcp/README.md` has a section titled
    "Why `env` is empty" arguing against precisely this, all three shipped
    configurations carry `"env": {}`, and the test pinning that covered the
    checked-in files and stopped where the product started handing one out.
29. **`docs/QUICKSTART.md` told a new user to command the agent by voice.** When
    this was found, the build carried arm, disarm and stop from a second process
    and no channel carried a goal, so the route named in section 7 answered «Не
    получилось.» — and the fix was to say so in the document. The channel has
    since been built and both halves of the defect are closed: the Local Core
    RPC link carries `goal.submit`, the voice companion routes a spoken goal
    over it as a typed `GoalRequest` (`plan.execute` asserted absent from the
    wire), `pz-agent voice run` wires that route,
    `tests/contract/test_voice_goal_e2e.py` crosses the seam — and the
    QUICKSTART passage now describes the route that exists.
30. **`voice run` wrote nothing to `logs/`** — defect 18's shape one package
    over, with `LOCAL_DEBUG_MAP.md` naming `logs/` for both voice symptoms
    including «стоп» heard while the character kept going.
31. **The standalone installer in `installer/` is reachable from nothing** — 948
    tested lines and a guide that reads as *the* install instructions, in no
    shipped artefact and run by nothing. Kept and labelled rather than deleted:
    it is the only path that works before anything is installed.
32. **AGENTS.md and CONTRIBUTING.md claimed CI rejects an empty exception
    handler.** No such check existed, for exactly the handler style this
    codebase writes, and it cannot be scanned honestly — the tree has typed
    handlers that fall through and typed handlers that deliberately swallow with
    their reasoning written above them. The untyped `except:` is scanned now;
    the swallow is a stated review rule. The scanner itself had no tests.

Numbers 10 to 32 are the reason this report states its base commit and
re-measures rather than carrying figures forward. A stale number is a small
lie; a document describing a safety gate that does not exist is a different
thing.

---

## 2. Confirmed game APIs

**None.** Against build 42.20, zero engine symbols are confirmed.

[`docs/GAME_API_VERIFICATION.md`](docs/GAME_API_VERIFICATION.md) lists every
symbol the mod assumes, each marked `requires_live` with an "Actual" column no
live run has ever filled in. The document was **rebuilt at this close from a
sweep of the sources themselves** — every `requires` list, every class handed
to `Toolkit.construct`, every accessor string, every direct global lookup,
every `Events.*` registration: 195 swept symbols, zero missing rows, and the
five rows that disagreed with their call sites corrected to match them
exactly. It now carries 120 symbol rows, all `requires_live`; its own text
records that the earlier revision carried about fifty rows, missed roughly
sixty symbols the mod touches — including the boot-path globals — and
disagreed with the code in five places. The capability probes report at best
`available_unverified` from a static scan of the install's own Lua; only a
live ack through `confirm()` promotes one to `verified`, and no live ack has
ever been produced.

Twelve probes, and three of them cannot reach `verified` from a scan at all:

| Capability | Ceiling without a live run | Why |
| --- | --- | --- |
| `survival_sleep` | `experimental` | Once the character is asleep there is no timed action to interrupt and no queue entry to cancel, so a panic stop cannot reach them. |
| `drink_world_source` | `experimental` | §12.4 lists the world water action as unconfirmed. |
| `autonomous_attack` | `unsupported` | Permanently. Listed so the report is explicit rather than silent. |

An `experimental` capability is upgradeable but not usable: its MCP tool is not
published and its action is refused. `pz_action_sleep` and
`pz_action_drink_source` are therefore normally absent from `list_tools`.

Two symbols deserve naming individually, because their failure modes are quiet:

- **`isClient` / `isServer`** — the no-multiplayer gate rests on these. If
  neither can be read, an ordinary single-player session refuses every mutating
  command, which is the correct conservative outcome and is indistinguishable at
  a glance from the agent being broken. They have their own section in
  `GAME_API_VERIFICATION.md` — "The multiplayer reading — check this before
  anything else" — for that reason.
- **`ISTakeWaterAction`** — three places in this repository once stated three
  different argument orders. The document now records the one the mod actually
  calls, `:new(character, waterObject, amount, item)`. A build that orders them
  differently fills the wrong thing and does not error.

---

## 3. Tests and their results

`bash scripts/check.sh` at this commit, every step — and it exits 0:

```
ruff format        ok      531 files already formatted
ruff lint          ok      all checks passed
mypy               ok      no issues found in 437 source files
forbidden patterns ok      no forbidden patterns found
version sync       ok      product=0.1.0 protocol=1.1 schema=1.0 mod=0.1.0
schema validity    ok      11 schema(s) valid
playbook in sync   ok      docs/LIVE_TEST_PLAYBOOK.md matches its 22 scenarios
pytest             ok      8581 passed, 5 skipped of 8586 collected
luacheck           ok      0 warnings / 0 errors in 75 files
lua tests          ok      4821 assertions across 32 suites, 0 failed
```

Both workflows are green against `f397d21`, the code tree these figures were
measured on (`c4320f0` differs from it only in the two control documents):
`windows package` run 31814473578 rebuilt and certified the release candidate
from that tree (§8), CI run 31814473620 is green for the same commit, and both verdicts
are recorded in `docs/control/STATUS.json` — whose reconciler refuses to call a
workflow green for any commit other than the one it actually ran against, and
whose gate refuses the whole plan if the tree has moved since. That gate is not
theoretical: it failed this branch once, on a commit pushed without its STATUS
reconciliation, and the sequence that prevents it is now written into
`AGENTS.md` rather than living only in the tooling.

**The two platforms disagree about skips, and that is expected.** Linux here
skips 5 of 8586; CI's Windows gate reports 8537 passed and **49** skipped of the
same 8586. The extra 44 are Windows-only suites' Linux counterparts and vice
versa, together with the two seam checks — the observation round trip and
`test_adapter_args_agreement` — which want a Lua interpreter the release runner
does not carry — neither run has a failure or an error, and the collected total is
identical, which is the number that says the two are looking at the same suite.

**The five skips, named rather than summarised.** One is a capability-tier
disagreement on `movement.move_to` — publishing P3 while its adapter declares
P2 and escalates per call — now pinned in
`tests/contract/test_mcp_action_coverage.py`; two are `test_mcp_server.py`
declining to test the no-SDK refusal because the MCP SDK *is* installed in
this environment; one is `test_teamon_bridge.py` on
`subprocess.CREATE_NO_WINDOW`, which exists only on Windows; and one is the
plan's own next-task check skipping with "every remote task is closed; there
is no next one to check" — the skip that marks the remote stage complete.
None is a missing optional dependency.

**On the Python matrix.** `.venv` runs 3.11.15 and that is the interpreter every
number above came from. `python3.12` exists in this container (3.12.11) but has
no pytest installed, so the suite was *not* run under it at this commit. CI
declares a 3.11/3.12 matrix in `.github/workflows/ci.yml`; that is
configuration, not a result observed here.

### What the suite is and is not

`tests/lua/` runs the mod's real modules under a plain Lua interpreter with fake
engine globals. It proves the mod's logic. It proves nothing about Build 42.20,
and its own docstrings say so.

`tests/contract/` is where this build's characteristic defect gets caught. It
holds forty seam files now; the first ten existed because ten seams were found
broken, each mutation-checked, because a seam test that would not have failed
is not evidence that the seam holds. The newest of them are this close's:
`test_sidecar_serves_the_core.py` reaches the real loop from a second OS
process through the exact client path `pz-agent-mcp` uses;
`test_remote_actions_served.py` watches a disarmed submit end `NOT_ARMED` and
an armed `action.wait` drain through the real engine to `SUCCEEDED`; and
`test_adapter_args_agreement.py` closes with the two-way census described in
§1.

`tests/unit/test_lua_observation_contract.py` runs the mod's observation builder
under `lua5.4` and puts its bytes through both gates the sidecar puts them
through — the JSON schema and `Observation.from_dict` — then parses every
reference it emitted with the Python implementation. That is the check that
matters most: a reference the two sides split differently does not raise, it
resolves to a different object.

---

## 4. Live scenarios: which ran, which did not

**None ran. All twenty-two are `NOT_RUN`.** Re-run at this commit:

```
$ pz-agent live-test status
live-test status /home/user/poject-zombigpt/evidence
----------------------------------------------------
  S01_INSTALL             NOT_RUN   -             never
  S02_HEARTBEAT           NOT_RUN   -             never
  ... twenty more rows, every one NOT_RUN, last run "never" ...

  PASS 0   FAIL 0   BLOCKED 0   NOT_RUN 22
  Nothing has been exercised. All 22 need a running game.
```

That trailing line used to read "All twenty" directly under a tally printing
22 — a hardcoded word that could not follow the list it described. It counts
the catalogue now, and it is worth naming because it is the smallest possible
instance of the defect this whole document is written against: a sentence that
was true when it was typed, read as current afterwards.

**Two scenario catalogues exist and their numbers collide.** This matters for
reading any evidence claim in this project, so it is stated here rather than
buried:

| Catalogue | Count | Driven by | Verdict decided by |
| --- | --- | --- | --- |
| `pz_agent_cli.livetest.scenarios` | 22 (`S01_INSTALL`…`S22_BUILD`) | `pz-agent live-test` | the runner, evaluating postconditions |
| `tests/game-smoke/` | 15 YAML plus `S99_endurance` | `pz-agent smoke` | a reviewer, reading prose assertions |

The same number means different things in each — `S06_drink.yaml` against
`S06_MANUAL_TAKEOVER`. **`scripts/check_release.py --release` enforces only the
first**, and every handoff document sends an operator only there.
`docs/RELEASE.md` asked for the second until this was reconciled, which meant a
human working the checklist and a machine working the gate were checking
different things. Neither catalogue is retired here: that is a decision about
what the release means, and it belongs with the person who will run them.

`pz-agent smoke --dry-run`, re-run at this commit, reports `blocked 16` and
writes, in as many words, that nothing was exercised. A dry run touched no
game, so every scenario is `BLOCKED` and the stamp records the build as
"(not detected — dry run)" rather than guessing.

---

## 5. Known limitations

Full list in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md). The ones that bear on
judging this release:

**No engine compatibility is claimed.** Mocks prove logic. Only §9 closes the
rest.

**The multiplayer refusal is untested against a real server.** It exists in
two places: the configuration key is a hard error, and
`ActionEngine._multiplayer_abort` refuses every mutating command unless the
mod positively reported single player. `observation.game.multiplayer` has
three states and **an absent reading is refused exactly as `true` is** —
silence is not permission, the same rule that stops a missing `is_bleeding`
from meaning "not bleeding". Stopping, disarming, cancelling and the three
read-only actions stay exempt, because an agent that cannot be stopped in the
one session it should not be running in is worse than no gate. Both halves
were mutation-checked. Nobody has watched it refuse a server.

**Remote plans are refused by design, not served.** `plan.execute` over the
Core RPC link answers a reasoned refusal recorded on the port: the loop's tick
thread cannot drive a multi-step plan to completion without holding its own
stop levers out of reach. The typed goal channel and the remote action channel
are the served routes; a client that wants a plan submits a goal.

**Reference generation is session-scoped, not save-scoped** as blueprint §3.7
specifies. In-session save/load invalidation works by the sidecar seeing
`save_id` change and closing the session — coarser, and stronger, because it
also closes in-flight commands as `lost`.

**MCP resource subscriptions are not delivered.** The server registers no
`subscribe_resource` handler, so every descriptor reports
`subscribable: false`. A client that could subscribe and was never notified
would read silence as "nothing changed" — the worst possible failure for the
safety view. Poll, and use the `seq` each read carries.

**`install-mod` needs a checkout or `--source`.** The wheel carries no Lua by
design; the sdist and the Windows package carry the mod.

**Which file 42.20 stores its version in is unknown.** Only the
`versionNumber=` header in `console.txt` is confirmed; the install-side
candidates are guesses. Detection reports an honest unknown rather than
substituting the target build.

**The Windows executables are built and certified — by CI, not here.**
`windows package` run 31252766042 compiled both with PyInstaller from
`8b9d0bd`, ran them with PATH reduced to the system directories, proved the
packaged pair speaks the Core RPC link (§8), and certified the archive.
Neither executable is signed. Nothing carried them back into this container:
the local `dist/` archive still ships without them, and no executable has ever
run on a machine that has the game (blocker L-003).

---

## 6. Exact commands to install and run

From a checkout:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

.venv\Scripts\pz-agent doctor            # read this before anything else
.venv\Scripts\pz-agent backup-save
.venv\Scripts\pz-agent install-mod
# enable "PZ Agent Bridge" in the game's mod list, load a TEST save
.venv\Scripts\pz-agent start             # attaches in OBSERVE
.venv\Scripts\pz-agent arm --mode assisted
```

From the release ZIP, which needs neither Python nor git:

```
install.bat
doctor.bat
backup-save.bat
run-live-tests.bat
```

The seventeen commands are `arm`, `backup-save`, `disarm`, `doctor`,
`install-mod`, `live-test`, `logs`, `remember`, `replay`, `restore-save`,
`smoke`, `start`, `status`, `stop`, `uninstall-mod`, `validate-config`, `voice`.
Full walkthrough: [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

---

## 7. The commit hash

`c4320f0` — a control-plane-only commit whose code tree is `f397d21`, the commit
both workflows ran against. Every figure above was measured with that tree
checked out; this document's own commit sits on top of it and changes nothing
but documentation. `git log c4320f0..HEAD --oneline` shows anything this
document does not cover.

`main`, `dev` and `claude/workflows-routines-docs-l294qp` all pointed at
`c4320f0` when it was measured. There is no divergence to reconcile and nothing stranded on a side
branch: the crafting and building wave, which had been living only on the third
of those, was merged into the line rather than force-replaced, so all of it is
in the release branch.

The previous revision reported R-008 open — **the shipped sidecar never served
the Core RPC router**, so `pz-agent start` published no link and a real
`pz-agent-mcp` found nothing to connect to, while every end-to-end test hosted
the router itself over fakes. It is closed: `pz_agent_cli.core_services`
implements the port bundle over the loop's real subsystems, `serve_core_rpc`
runs on start and comes down in the loop's own `finally`, and
`tests/contract/test_sidecar_serves_the_core.py` reaches the real core from a
second OS process through the exact client path the MCP server uses. The same
criterion audit had marked the recorded progress down from 74.26% to 59.66% by
refusing 22 heavy claims whose tests did not observe them; all 56 affected
tasks were **re-verified rather than inherited** — `scripts/verify_carryover.py`
ran every named test here and now, 56 of 56 confirmed — which is how the figure
in §1 was earned back. `docs/control/BLOCKERS.md` carries every record.

---

## 8. The release artefact and its checksum

**The artefact of record is CI's.** `windows package` run 31814473578 built
both executables from `f397d21` — this head's code tree — and **CERTIFIED
v1.0.0-rc1**:

```
pz-agent-windows-v1.0.0-rc1.zip  (CI artifact pz-agent-windows-rc, id 9225004153)
  sha256   8be6712c6bce60f97b0f44e3c85d28348e67bba3d9619c1044b33ef7cb8276c1
  entries  77 — the 76 digested files plus BUILD-MANIFEST.json
```

**Read that digest from the gate, not from the artifacts API.** The API — and
the workflow log's own upload step — report the SHA-256 of the *upload wrapper*
zip, which for this run is `c45b61b4…` and is not the archive. The archive's own
digest appears in exactly two places, `build_rc.py`'s summary and the gate's
`[ok  ] archive:` line, and they agree. Anyone re-deriving this number should
check which of the two they are holding.

Certification means every gate rule green: archive complete, all 11 wrappers at
the root, both executables in `bin/`, 76 file digests matching, **8537 of 8586
tests passed with no failures and no errors** (49 skipped), 31 MCP end-to-end
testcases run and passed, and the archive claiming no live-test evidence. It
also includes the workflow step in which the **packaged `pz-agent.exe` serves
the Core RPC link for real and the packaged `pz-agent-mcp.exe` completes a
JSON-RPC `initialize` through it** — both with PATH reduced to the system
directories, no Python reachable.

The archive has been 77 entries since the crafting and building wave; what has
moved since is the suite, 8508 → 8586, as the observation-seam round trip and
the plan's parsed command lines were added.

`docs/control/STATUS.json` records this RC `CURRENT`; by its own rule, any code
commit after `f397d21` makes it STALE until the workflow rebuilds it. Note that
the build is **not** reproducible — identical inputs produce different digests —
so this hash identifies *that run*, not the tree. That is why the RC is stated
as archive plus source commit plus run, and why naming one of the three is not
enough to identify it.

The local archive is the honest record of what this container can produce —
no PyInstaller output, so no executables. Re-verified at this commit with a
fresh `pytest --junitxml` run fed to `scripts/check_release.py --rc`:

```
dist/pz-agent-windows-v1.0.0-rc1.zip
  sha256   fdf277685ca11e6b8bea0e5a7ec9925c84ad0dd75e31fd4f5368428a3fbaa76c
  size     488 741 bytes
  entries  75 (74 files plus BUILD-MANIFEST.json)

[ok  ] archive:          pz-agent-windows-v1.0.0-rc1.zip, 75 entr(ies)
[FAIL] archive.complete: the archive declares 2 missing file(s): bin/pz-agent.exe, bin/pz-agent-mcp.exe
[ok  ] archive.claims:   the archive claims no live-test evidence
[ok  ] archive.bat:      all 11 wrappers are at the root
[FAIL] archive.bin:      missing from bin/: pz-agent.exe, pz-agent-mcp.exe
[ok  ] archive.digests:  74 file(s) match the digests recorded for them
[ok  ] tests:            8581 of 8586 test(s) passed, no failures and no errors; 5 skipped
[ok  ] tests.mcp-e2e:    31 testcase(s) from tests.contract.test_mcp_subprocess_e2e ran and passed

REFUSED v1.0.0-rc1: 2 of 8 check(s) failed.
```

That refusal is correct — the gate refusing the executable-less local build is
the gate working, and the same gate passing on CI's build is why the archive
of record is CI's.

`check_release.py --release` refuses additionally on
`release/evidence-manifest.json`, which does not exist and is produced by
`pz-agent live-test finalize` and by nothing else. That refusal is also the
gate working.

No wheel or sdist was built at this commit. The pair in `dist/` still matches
`dist/SHA256SUMS` (`fe649932…`, `e1f6d665…` — re-hashed here), but both were
built dozens of commits ago; they describe that tree, not this one.

---

## 9. Every step that physically requires launching the game

This is the list the whole report exists for. Each item is blocked on a running
Project Zomboid Build 42.20 on Windows, and on nothing else. It is the same
list `docs/LOCAL_AGENT_PROMPT.md` and `docs/LOCAL_GAME_HANDOFF.md` hand to the
operator; only then may `v1.0.0` exist.

### Once, before anything

1. Take a backup and use a **dedicated test save**. `pz-agent live-test prepare`
   refuses without `--save <mode>/<name>` — there is no default, because
   guessing which world to experiment on is how a main save gets used.
2. Run `pz-agent doctor` against a real installation. This is what turns every
   capability from unprobed into a state backed by evidence, and it settles
   which file the build version lives in.
3. Confirm `install-mod` places the bridge where the game finds it and that
   "PZ Agent Bridge" appears and enables in the in-game mod list.
4. Confirm the mod writes `heartbeat.game.json` once a save is loaded.

### The two symbols to confirm before concluding anything is broken

5. **`isClient` / `isServer`.** If these cannot be read, the agent refuses every
   mutating command in a perfectly ordinary single-player session. Check them
   during S02 before diagnosing anything else.
6. **`ISTakeWaterAction`'s argument order.** A wrong order fills the wrong thing
   and does not error.

### Things that will fail, and are not the session's fault

These are known before the operator starts, reproduced from the sources on both
sides of the seam, and recorded in
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md). A live session should not spend
its time diagnosing them.

**Each of the three is now demonstrated by a test rather than argued from two
files.** `tests/contract/test_observation_document_round_trip.py` builds one
observation through the mod's own readers and `ObserveModel.build`, decodes it
with the sidecar's own `Observation.from_dict`, and asserts what each consumer
then reads. Run it before the session if you want to see the failures without a
game: `pytest tests/contract/test_observation_document_round_trip.py -v`. That
matters here because the previous revision of this section led with two *larger*
claims — "the agent will not walk" and "`build_structure` will refuse every
placement" — and both were wrong, produced by a checker that searched the
sources for a spelling the mod does not use. The square tier is real:
`ObserveModel.buildSquare` mints `kind = "square"` entries, `mergeNearby` folds
them into `nearby.objects`, and both movement and the enclosure check find them.
What is left is smaller, and this time it is executable:

6a. **A floor-changing move will always refuse `PATH_NOT_FOUND`.**
    `movement._check_square` requires the `stairs` semantic on the *square*
    entry; the mod puts it on the staircase *object* standing there, on purpose.
    The gate runs toward caution, so this costs journeys between storeys and
    nothing else.
    *Demonstrated by* `test_the_two_square_semantics_the_sidecar_reads_off_the_square`.

6b. **A square behind a closed window reports the wrong refusal.** The dedicated
    `closed_window` / `POLICY_DENIED` branch never fires — no such token is
    produced anywhere — but the square reads as not passable and is refused as
    `blocked`. The refusal stands; only its name is wrong.
    *Demonstrated by the same test*, which asserts both tokens absent from every
    square the mod built.

6c. **Nothing will loot a world container.** A nearby crate is minted a
    reference the planner can name, but `resolve_container` searches only
    `inventory.containers`, which the crate is never added to, so
    `container.inspect` and `inventory.transfer` refuse `INVALID_REF`. The
    mission can now reach the crate and is refused at the crate instead of
    before it.
    *Demonstrated by* `test_a_crate_the_planner_can_name_and_nothing_can_open`,
    which asserts both halves — the crate is nameable, and it is unresolvable —
    because the gap is precisely the distance between them.

6c is the one that still costs a whole goal kind, and it is the highest-value
thing a live session can settle: the missing half is a mod-side inventory tier
for an open world container, and what it costs to read every tick is a question
only a running game answers.

**What the round trip does not do.** It runs the mod's Lua against fakes that
stand in for the engine, so it proves the two sides agree about the document.
It proves nothing about whether the engine answers those accessors at all on
Build 42.20 — that is §2's list, and it is what the live session is for.

### The twenty-two live scenarios

7. `S01_INSTALL` through `S22`, via `run-live-tests.bat` — twenty-two since the
   crafting and building wave added S21 and S22 to give both new capabilities a
   live route.
   Per-scenario detail — world preparation, required starting state, the exact
   command, what the human does in-game, and the postconditions that decide the
   verdict — is in [`docs/LIVE_TEST_PLAYBOOK.md`](docs/LIVE_TEST_PLAYBOOK.md),
   which is generated from the same table the runner executes.

   Declared time budget across all twenty-two: **5 h 41 min** — 20 460 seconds,
   re-summed from the playbook's own `**Time budget:**` lines at this commit,
   not adjusted from the previous revision's twenty-scenario figure. Of that,
   S19 (30 minutes unattended) and S20 (2 hours) are the endurance runs that
   close T029. Several scenarios need a deliberately awkward setup — a
   player-queued action running alongside a mod-queued one, a zombie allowed to
   notice the character mid-read — and the playbook says which.

   Expect `S11_CONTAINER` to fail on 6c, and any leg that changes storey on 6a.
   Every scenario is worth running regardless: what they produce is the
   observation document the mod actually sends, and this branch has now twice
   been wrong about that document while reading only the sources.

8. **Measured p50/p95 latencies.** Only the three scenarios flagged
   `measures_latency` record them. Any number produced without running them
   would be invented.

### Things only a live run can settle

9. **Every engine symbol in `docs/GAME_API_VERIFICATION.md`** — 120 rows
   marked `requires_live`, rebuilt at this close from a 195-symbol sweep of
   the sources (§2). Note that `grep -rn "Build 42:" pz-mod/` returns 6 lines,
   about a twentieth of the surface, so it is *not* a complete list of the
   guesses — the document is. (The handoff documents still quote the earlier
   revision's 52-row count; the rebuilt document supersedes it, and the
   instruction they give — work the document, row by row — is unchanged.)
10. **`BackupManager.restore`'s game-running probe.** It reports "may be
    running" when it cannot tell, and that conservative answer needs confirming
    against a real Project Zomboid process name. A wrong answer here is the one
    that corrupts a save.
11. **The multiplayer refusal against an actual server.** Tested against fakes
    only.
12. **The remote surfaces against the game.** The Core RPC link, the remote
    action channel, the goal channel and the voice route are proven between
    real OS processes with the mod faked at the exchange files — never with
    the game on the other side of those files.
13. **`release/evidence-manifest.json`**, via `pz-agent live-test finalize`.
    Nothing else produces it — not a build, not a green test suite, not the
    certified RC.

### Then, and only then

14. `scripts/check_release.py --release` must stop refusing — the RC gate
    already passes on CI's build, and the `--release` gate's remaining refusal
    is the evidence manifest above.
15. Merge to `main`, tag, and cut the release. **Do not tag `v1.0.0` before
    step 14 passes** (`docs/control/BLOCKERS.md` RB-002 states the same rule),
    and note that every version constant currently says `0.1.0`.

---

## 10. What this report does not say

It does not say the architecture is ready and only needs testing. It does not
say a user can take it from here.

It says: the remote stage is complete by its own weighted plan — 73.3%
overall (2305 of 3144 weight, 400 of 484 tasks), with every band a container
can move at 100.0 and the two the game owns at zero; the tree is covered by
8586 Python tests and 4821 Lua assertions across 32 suites — 8581 passed and 5
named skips here, 8537 passed and 49 skips on CI's Windows leg of the same
8586, with `scripts/check.sh` exiting 0 and both workflows green against this
head's code tree; the release candidate was rebuilt and certified by CI from
that same tree, its packaged executables proving the served link between
themselves; and §9 is the complete list of what a running game — and nothing
else — still has to settle.

It also says something the previous revisions could not: **the tests pass
because each side of the seam is tested against its own idea of the observation
document, so "8581 tests passed" is not "the agent plays the game".** Three
known gaps remain — a floor-changing move always refuses, a closed-window square
is refused under the wrong name, and nothing loots a world container. Each is a
refusal rather than a crash. §9 6a–6c states them; `LIMITATIONS.md` carries the
measurements; `tests/contract/test_gates_without_producers.py` fails if any is
quietly closed or quietly widened.

And it says one thing about its own method. A previous revision of this document
asserted three *larger* gaps — that the agent could not walk at all and that
`build_structure` refused every placement — on the strength of a contract check
that searched the mod for a literal spelling the mod does not use. The producer
had been there the whole time, one file over, behind a named constant. The
checker now carries a control that fails if it cannot see the mod's own idiom.
The lesson is the document's own subject matter: a claim about the far side of a
seam, made without running the seam, is a hypothesis — and this one was stated
as a finding twice before it was caught.

Where a claim could not be checked, this report says so rather than rounding up.
That is the same rule the code follows: success means a postcondition was
observed.
