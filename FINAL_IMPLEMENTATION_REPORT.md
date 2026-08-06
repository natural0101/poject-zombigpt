# Final implementation report — pz-agent

Prepared to the gate in [`docs/RELEASE.md`](docs/RELEASE.md), whose "The final
report" section lists nine things this document must state. They are §1 to §9
below, in that order.

**Base commit:** `dev` at `f5e8441` (see below — it is refreshed each time this file is)
**Versions:** product 0.1.0 · protocol 1.1 · schema 1.0 · mod 0.1.0 · supported build 42.20

A report cannot name the commit that contains it — the hash does not exist until
the commit is made. The hash above is this report's parent. Check
`git log f5e8441..HEAD` before trusting any number here against a newer tree.

**Note the version.** The release candidate is named `v1.0.0-rc1`, and every
version constant in the tree says `0.1.0`. No `1.0.0` exists in `version.py`,
`pyproject.toml` or either `mod.info`, and no git tag exists at all. The RC
filename is a target, not a state.

Every figure below was produced by running something at this commit. The
previous revision of this document was written against `main` at `6a57f74`, 36
commits back, and had drifted badly: it claimed 2338 Python tests (there are
3508), 1269 Lua assertions (2864), 202 mypy files (262), 7 schemas (6), 30
luacheck files (62), nine registered adapters (19) and an installer that placed
17 files (30). None of that was dishonest when written. All of it was wrong by
the time anyone read it, which is why this revision states its base commit at
the top and why the numbers were re-measured rather than carried over.

---

## The short version

Twenty-eight of the thirty tasks in
[`docs/blueprint/task_graph.yaml`](docs/blueprint/task_graph.yaml) are complete,
tested and documented. The two that are not — the endurance run and this
release's evidence — **cannot be completed in this environment, because there is
no Project Zomboid installation here.** They are not deferred for convenience;
they are physically blocked, and §9 is the complete list of what closing them
requires.

Nothing in this repository claims to have been verified against the engine. Not
one engine symbol has been confirmed against a running game. All twenty
live-test scenarios are `NOT_RUN`, and the runner's initial state is `NOT_RUN`
precisely so a scenario nobody ran cannot report a pass.

---

## 1. What is implemented, by task id

| Task | Title | Status |
| --- | --- | --- |
| T001–T028 | The full graph through the live smoke harness | **done** |
| T029 | Endurance run | **blocked on a live game** |
| T030 | Release artefact and evidence | **blocked on T029** |

`docs/PROGRESS.md` carries the per-task table and the deviations. Work beyond
the original graph — the mod's command executor, the seventeen game adapters,
the model-backed planners, the live-test harness, the Windows RC and the
handoff documentation — is recorded there under "The playable-agent branch".

Measured at this commit:

| Surface | Count |
| --- | --- |
| `ActionName` members | 22 (17 owned by the mod's adapter files, 5 control plane) |
| Adapters in the assembled registry | 19 |
| MCP tools | 31, of which 19 submit an action |
| MCP resources | 7, none subscribable |
| CLI commands | 17 |
| Capability probes | 12 |
| Live-test scenarios | 20 |

### Fourteen defects, found and closed

Every subsystem in this build was written, tested and green. What nothing tested
was whether the subsystems were *connected*, or whether the documents describing
them were true. Fourteen defects of those two shapes were found, each by a test
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

**Three were documents asserting things the code did not do**, which is the
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
    lines in two files against 52 `requires_live` rows — about an eighth. An
    agent following that would have checked six symbols and concluded the
    unconfirmed surface was covered.
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

Numbers 10 to 14 are the reason this report states its base commit and
re-measures rather than carrying figures forward. A stale number is a small
lie; a document describing a safety gate that does not exist is a different
thing.

---

## 2. Confirmed game APIs

**None.** Against build 42.20, zero engine symbols are confirmed.

[`docs/GAME_API_VERIFICATION.md`](docs/GAME_API_VERIFICATION.md) lists every
symbol the mod assumes, each marked `requires_live` with an empty "Actual"
column. The capability probes report at best `available_unverified` from a
static scan of the install's own Lua; only a live ack through `confirm()`
promotes one to `verified`, and no live ack has ever been produced.

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
  a glance from the agent being broken. They are the first rows in
  `GAME_API_VERIFICATION.md` for that reason.
- **`ISTakeWaterAction`** — three places in this repository once stated three
  different argument orders. The document now records the one the mod actually
  calls, `:new(character, waterObject, amount, item)`. A build that orders them
  differently fills the wrong thing and does not error.

---

## 3. Tests and their results

`scripts/check.sh` at this commit, every step:

```
ruff format        ok    316 files already formatted
ruff lint          ok    All checks passed!
mypy               ok    no issues found in 262 source files
forbidden patterns ok    no stub bodies, no TODO markers, no eval/exec/loadstring, no secrets
version sync       ok    product=0.1.0 protocol=1.1 schema=1.0 mod=0.1.0
schema validity    ok    6 schema(s) valid
playbook in sync   ok    docs/LIVE_TEST_PLAYBOOK.md matches its 20 scenarios
pytest             ok    3508 passed, 2 skipped
luacheck           ok    0 warnings / 0 errors in 62 files
lua tests          ok    2864 assertions across 26 suites, 0 failed
```

**The two skips, named rather than summarised.** One is a capability-tier
disagreement on `movement.move_to` in `test_mcp_action_coverage.py`; one is
`test_capabilities_scanner.py` declining to assert that file permissions deny a
read, because the suite runs as root. Neither is a missing optional dependency.

**On the Python matrix.** `.venv` runs 3.11.15 and that is the interpreter every
number above came from. `python3.12` exists in this container but has no pytest
installed, so the suite was *not* run under it at this commit. CI declares a
3.11/3.12 matrix in `.github/workflows/ci.yml`; that is configuration, not a
result observed here.

### What the suite is and is not

`tests/lua/` runs the mod's real modules under a plain Lua interpreter with fake
engine globals. It proves the mod's logic. It proves nothing about Build 42.20,
and its own docstrings say so.

`tests/contract/` is where this build's characteristic defect gets caught. Ten
seam tests exist because ten seams were found broken; each was mutation-checked,
because a seam test that would not have failed is not evidence that the seam
holds.

`tests/unit/test_lua_observation_contract.py` runs the mod's observation builder
under `lua5.4` and puts its bytes through both gates the sidecar puts them
through — the JSON schema and `Observation.from_dict` — then parses every
reference it emitted with the Python implementation. That is the check that
matters most: a reference the two sides split differently does not raise, it
resolves to a different object.

---

## 4. Live scenarios: which ran, which did not

**None ran. All twenty are `NOT_RUN`.**

```
$ pz-agent live-test status
live-test status /home/user/poject-zombigpt/evidence
----------------------------------------------------
  S01_INSTALL             NOT_RUN   -             never
  S02_HEARTBEAT           NOT_RUN   -             never
  ... eighteen more rows, every one NOT_RUN, last run "never" ...
```

**Two scenario catalogues exist and their numbers collide.** This matters for
reading any evidence claim in this project, so it is stated here rather than
buried:

| Catalogue | Count | Driven by | Verdict decided by |
| --- | --- | --- | --- |
| `pz_agent_cli.livetest.scenarios` | 20 (`S01_INSTALL`…`S20_AUTONOMOUS_2_HOURS`) | `pz-agent live-test` | the runner, evaluating postconditions |
| `tests/game-smoke/` | 15 YAML plus `S99_endurance` | `pz-agent smoke` | a reviewer, reading prose assertions |

The same number means different things in each — `S06_drink.yaml` against
`S06_MANUAL_TAKEOVER`. **`scripts/check_release.py --release` enforces only the
first**, and every handoff document sends an operator only there.
`docs/RELEASE.md` asked for the second until this commit, which meant a human
working the checklist and a machine working the gate were checking different
things. Neither catalogue is retired here: that is a decision about what the
release means, and it belongs with the person who will run them.

`pz-agent smoke --dry-run` reports `blocked 16` and writes, in as many words,
that nothing was exercised. A dry run touched no game, so every scenario is
`BLOCKED` and the stamp records the build as "(not detected — dry run)" rather
than guessing.

---

## 5. Known limitations

Full list in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md). The ones that bear on
judging this release:

**No engine compatibility is claimed.** Mocks prove logic. Only §9 closes the
rest.

**Multiplayer refusal is new, and untested against a real server.** Until this
commit the documented protection did not exist. It does now, in two places: the
configuration key is a hard error, and `ActionEngine._multiplayer_abort` refuses
every mutating command unless the mod positively reported single player.
`observation.game.multiplayer` has three states and **an absent reading is
refused exactly as `true` is** — silence is not permission, the same rule that
stops a missing `is_bleeding` from meaning "not bleeding". Stopping, disarming,
cancelling and the three read-only actions stay exempt, because an agent that
cannot be stopped in the one session it should not be running in is worse than
no gate. Both halves were mutation-checked. Nobody has watched it refuse a
server.

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

**The Windows executables have never been built.** `docs/LIMITATIONS.md` warns
that the launcher is unsigned; it has not been signed because it has not been
compiled. See §8.

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

`f5e8441` on `dev`. See the header for why this is the parent rather than the
containing commit; `git log f5e8441..HEAD --oneline` shows anything this
document does not cover.

---

## 8. The release artefact and its checksum

```
dist/pz-agent-windows-v1.0.0-rc1.zip
  sha256   1f8f1f163744bc5a4f0a791ecc4c557c88837cbf320a0a5b669502230a32762a
  size     259 035 bytes
  entries  66 (65 files plus BUILD-MANIFEST.json)
```

**It is marked INCOMPLETE and it is not a release candidate by this project's
own gate.** `BUILD-MANIFEST.json` records `complete: false`, `build_rc.py` exits
1, and `scripts/check_release.py --rc` refuses:

```
[FAIL] archive.complete: the archive declares 2 missing file(s): bin/pz-agent.exe, bin/pz-agent-mcp.exe
[FAIL] archive.bin:      missing from bin/: pz-agent.exe, pz-agent-mcp.exe
[ok  ] archive.bat:      all 11 wrappers are at the root
[ok  ] archive.digests:  65 file(s) match the digests recorded for them
[ok  ] archive.claims:   the archive claims no live-test evidence
[ok  ] tests:            3508 of 3510 passed, 2 skipped
```

Both executables need PyInstaller on Windows. `.github/workflows/windows.yml`
builds them on `windows-latest`; nothing in this Linux container can.

`check_release.py --release` refuses additionally on
`release/evidence-manifest.json`, which does not exist and is produced by
`pz-agent live-test finalize` and by nothing else. That refusal is the gate
working.

No wheel or sdist was built at this commit: `uv` is not installed in this
container. The hashes in the previous revision of this report
(`fe649932…`, `e1f6d665…`) described artefacts from a build 34 commits ago and
have been removed rather than repeated.

---

## 9. Every step that physically requires launching the game

This is the list the whole report exists for. Each item is blocked on a running
Project Zomboid Build 42.20 on Windows, and on nothing else.

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

### The twenty live scenarios

7. `S01_INSTALL` through `S20_AUTONOMOUS_2_HOURS`, via `run-live-tests.bat`.
   Per-scenario detail — world preparation, required starting state, the exact
   command, what the human does in-game, and the postconditions that decide the
   verdict — is in [`docs/LIVE_TEST_PLAYBOOK.md`](docs/LIVE_TEST_PLAYBOOK.md),
   which is generated from the same table the runner executes.

   Declared time budget across all twenty: **5 h 16 min**, of which S19
   (30 minutes unattended) and S20 (2 hours) are the endurance runs that close
   T029. Several scenarios need a deliberately awkward setup — a player-queued
   action running alongside a mod-queued one, a zombie allowed to notice the
   character mid-read — and the playbook says which.

8. **Measured p50/p95 latencies.** Only the scenarios flagged `measures_latency`
   record them. Any number produced without running them would be invented.

### Things only a live run can settle

9. **Every engine symbol in `docs/GAME_API_VERIFICATION.md`** — 48 rows marked
   `requires_live`. Note that `grep -rn "Build 42:" pz-mod/` returns 6 lines
   covering roughly 8 symbols, so it is *not* a complete list of the guesses;
   the document is.
10. **`BackupManager.restore`'s game-running probe.** It reports "may be
    running" when it cannot tell, and that conservative answer needs confirming
    against a real Project Zomboid process name. A wrong answer here is the one
    that corrupts a save.
11. **The multiplayer refusal against an actual server.** Tested against fakes
    only.
12. **`release/evidence-manifest.json`**, via `pz-agent live-test finalize`.
    Nothing else produces it — not a build, not a green test suite.

### Then, and only then

13. Build the two executables with PyInstaller on Windows and re-run
    `packaging/windows/build_rc.py`.
14. `scripts/check_release.py --release` must stop refusing.
15. Merge to `main`, tag, and cut the release. **Do not tag `v1.0.0` before
    step 14 passes**, and note that every version constant currently says
    `0.1.0`.

---

## 10. What this report does not say

It does not say the architecture is ready and only needs testing. It does not
say a user can take it from here.

It says: twenty-eight tasks are implemented and covered by 3508 Python tests and
2864 Lua assertions; ten wiring defects were found by seam tests and closed, one
of them a safety gate that had been documented for weeks and never written; two
tasks are blocked on a game that does not exist in this environment; and §9 is
the complete list of what closing them requires.

Where a claim could not be checked, this report says so rather than rounding up.
That is the same rule the code follows: success means a postcondition was
observed.
