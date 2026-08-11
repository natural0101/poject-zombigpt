# Troubleshooting

Start here:

```powershell
.venv\Scripts\pz-agent doctor
```

Every check has a stable code and remediation text. This document listed none
of them for a while, which made "look it up in TROUBLESHOOTING.md" impossible
advice for anyone holding a code.

## The doctor codes

Ten checks, in the order `doctor` runs them. Each depends on the ones above it,
so **fix the first failure before reading the rest** — a `PZD004` that says
"no Zomboid directory to test" is not a permissions problem, it is `PZD003`
still unfixed.

| Code | Check | What it means when it fails |
| --- | --- | --- |
| `PZD001` | `game_installation` | The Project Zomboid install could not be found. Discovery searches the Steam library folders and the usual paths; the message says how many it searched. Set `game.install_dir` in `config.toml` if it lives somewhere unusual. |
| `PZD002` | `build_detected` | The build number could not be read. This is a **warning**, not a failure: only the `versionNumber=` header in `console.txt` is confirmed, and the install-side candidates are guesses. An honest unknown is reported rather than the target build being substituted. |
| `PZD003` | `user_directory` | The `Zomboid` user directory could not be found — the one holding `Saves/`, `mods/` and `console.txt`, not the install. On Windows it follows `USERPROFILE` and OneDrive redirection. Set `game.user_dir` to override. |
| `PZD004` | `directory_permissions` | The Zomboid directory exists but could not be written to. Fixing `PZD003` first is usually the answer; permissions cannot be tested on a directory that was never located. |
| `PZD005` | `mod_installed` | The bridge mod is not in the mods folder. Run `pz-agent install-mod`. Present on disk is not the same as loaded — see `PZD006`. |
| `PZD006` | `game_heartbeat` | No `heartbeat.game.json`, or one whose timestamp is not advancing. This is the check that distinguishes "the mod is installed" from "the mod is running": a mod that threw during load looks identical to an idle exchange directory everywhere except here. Enable **PZ Agent Bridge** in the in-game mod list and load a save. A fourth cause has its own remediation: a heartbeat stamped *ahead* of this machine's clock is refused as describing no moment on it, and the answer there is the system clock and time synchronisation, not the mod list. |
| `PZD007` | `ipc_writable` | The exchange directory could not be written to, so the sidecar could not send a command even if everything else were healthy. |
| `PZD008` | `timed_actions` | The timed-action classes the adapters construct could not be found by a scan of the install's own Lua. This is what turns capabilities from unprobed into a state backed by evidence; a failure here explains a later `CAPABILITY_UNAVAILABLE`. |
| `PZD009` | `conflicting_files` | Something is left over in the exchange directory that this build did not write — usually a previous version's journals. |
| `PZD010` | `active_session` | Reports whether a session is open and which mode it is in. `unknown` with no exchange directory is normal before the first run. A session is called active only when the game's heartbeat *names it and is current*: a matching id in a heartbeat that has gone silent is reported as the last word of a game that is no longer running, because a crashed game leaves a file carrying exactly that id. |

A check can report `pass`, `info`, `warn`, `fail` or `unknown` — `info` is a
fact rather than a verdict ("no session is attached" is neither healthy nor
unhealthy), and **`unknown` is not a pass**. It means the check could not be performed — almost always because
something above it failed — and `doctor` says which.

---

## The MCP server's exit codes

`pz-agent-mcp` is launched as a subprocess by your MCP client, so when it
refuses, what you see in the client's log is an exit code and one line on
stderr. There are eleven, all declared as `EXIT_*` constants in
`packages/pz_agent_mcp/src/pz_agent_mcp/__main__.py`, and they are eleven
rather than fewer because the remedies are different: "no sidecar is running",
"the sidecar died without cleaning up", "there are two installs on this
machine", "the SDK extra is missing", "the SDK is the wrong version" and "the
server itself failed" send you to six different places.

| Code | Constant | Cause | Remedy |
| --- | --- | --- | --- |
| 0 | `EXIT_OK` | Served the surface, or answered `--describe` / `--version` / `--help`. | — |
| 1 | `EXIT_NOT_WIRED` | No sidecar for that state directory: either its `runtime/` folder holds no `core-rpc.json`, or the descriptor names a live process and nothing answered on its address within the deadline. Both mean the sidecar is not running. | `pz-agent start`, then `pz-agent status`. |
| 2 | `EXIT_USAGE` | A malformed invocation. Passing both `--state-dir` and `--zomboid-dir` lands here — they are two answers to one question, and a precedence rule would leave you certain you had set the one that was ignored. So does passing either alongside embedder-supplied services, and so does a directory flag naming a directory that is not there. | Read `--help`. `pz-agent start` prints the client configuration block with `--state-dir` already filled in. |
| 3 | `EXIT_NO_SDK` | The optional `mcp` extra is not installed. This gate fires **before** anything to do with the sidecar, so it is what you meet on a fresh install — not exit 1. | `pip install pz-agent[mcp]`. |
| 4 | `EXIT_STALE_DESCRIPTOR` | `core-rpc.json` is there and the process it names is gone, or its token file went with it. The sidecar was killed rather than stopped. | `pz-agent start` again. If it keeps stopping, `pz-agent doctor` reports why. |
| 5 | `EXIT_PROTOCOL_MISMATCH` | The descriptor says the sidecar speaks a different Core RPC major than this executable does. Both halves ship from one install, so two installs are present. | Run each half with `--version` — the `pz-agent` command and the `pz-agent-mcp` command — and use the pair that match. Restarting the sidecar cannot fix this. |
| 6 | `EXIT_DESCRIPTOR_UNREADABLE` | There is a file where the descriptor belongs and it is not one: truncated, foreign, or left by a start that did not finish. | `pz-agent stop` then `pz-agent start` rewrites it. Restarting a running sidecar will not. |
| 7 | `EXIT_ANSWER_UNREADABLE` | Something answered on the sidecar's address and this build cannot read the answer. Never reported as a refusal: the core did not say no, this side could not tell what it said. | Check both versions match, then `pz-agent doctor`. |
| 8 | `EXIT_NO_STATE_DIR` | Neither directory flag was given and the process cannot work out which state directory to use — either the CLI package that owns the layout is not importable (an incomplete install), or the machine's directories could not be read. | Name it with `--state-dir`. `pz-agent doctor` reports what is unreadable. |
| 9 | `EXIT_SDK_INCOMPATIBLE` | The `mcp` extra is installed and is not a version this build can drive — it needs the 2.x server API. Distinct from exit 3 because the remedy is a version constraint rather than an install: `pip install pz-agent[mcp]` would not fix it. | `pip install "mcp>=2,<3"`. |
| 10 | `EXIT_SERVER_FAILED` | An exception escaped the server's build or its serve loop after every named refusal had its chance — a bug in the server, or an SDK that kept its constructor signature and changed behind it. | Read the one stderr line naming the exception, and report it with that line. Nothing you can install or restart fixes a bug. |

Codes 3, 8 and 9 fire before anything is dialled. Codes 4 to 7 mean the link
was tried. `tests/contract/test_mcp_exit_codes_documented.py` reads the
`EXIT_*` constants out of the module and fails when `configs/mcp/README.md`
leaves one of them undocumented — that table cannot fall behind the executable.
This one carries no such guard and is kept current by hand; when the two
disagree, believe that file.

Two invocations never reach any of that machinery and answer on a machine with
no game, no sidecar and no SDK:

```
pz-agent-mcp --describe
```

That writes the whole published surface as JSON. `--version` answers the same
way. If those work and nothing else does, the executable is fine and the problem
is the link.

**Nothing but protocol reaches stdout.** Once serving starts, stdout is the
JSON-RPC stream your client is parsing. Every diagnostic goes to stderr, and the
one line written on a successful connection names the server and the version and
nothing else. The address and the token never reach either stream.

---

## Refusals you will actually see

Every failure the agent reports carries a `reason_code` from a closed set
(the full list is in [`PROTOCOL.md`](PROTOCOL.md)). The ones below are the ones
with a remedy that is not obvious. The rest of this document expands the
common ones.

| Reason code | What it means | What to do |
| --- | --- | --- |
| `NOT_ARMED` | The session is in `OBSERVE` or `OFF` and the tool changes the world. | `pz-agent arm --mode assisted`, or use a read/query tool. Stopping, disarming and cancelling are never refused this way. |
| `POLICY_DENIED` | The mode, the initiative or the risk tier does not allow it. A `P4` action on the agent's own initiative always lands here. | Ask for it explicitly; a `P4` needs a per-call grant. |
| `CAPABILITY_UNAVAILABLE` | The capability behind the action is not usable on this install. | See *"Capability unsupported"* below. |
| `INVALID_REF` | A reference from a previous session or save generation. | Re-observe. Not retryable. |
| `INVALID_ARGUMENT` | The value failed the published schema or the adapter's own check. | Compare against `docs/MCP_TOOLS.md`; the bounds there are the enforced ones. |
| `PRECONDITION_FAILED` | The world was not in the state the action needs — bandaging a part that is not bleeding, for instance. | Re-observe and pick again. |
| `QUEUE_REJECTED` | The mod would not take the command; usually one is already in flight. | Retryable. Wait for the terminal ack. |
| `LEASE_EXPIRED` | The command's TTL ran out. | See below. |
| `PATH_NOT_FOUND` / `TARGET_OUT_OF_RANGE` / `TARGET_NOT_LOADED` | The destination cannot be reached, is beyond `max_distance`, or is in a chunk the game has not loaded. | Move closer and re-observe. Not retryable. |
| `PATH_STUCK` / `NO_PROGRESS` | The character stopped making progress. | Retryable within the budget; then reported. |
| `ACTION_TIMEOUT` | The action did not reach a terminal state inside its lease. | Retryable. |
| `POSTCONDITION_FAILED` | See below — this is the system working. |  |
| `NO_SAFE_FOOD` / `NO_SAFE_DRINK` / `NO_SUITABLE_LITERATURE` | Policy rejected every candidate, and says why each one lost. | See below. |
| `RESOURCE_RESERVED` | The only candidate is one you reserved. | `pz-agent remember release <item>`. |
| `CONTAINER_FULL` | The destination has no room. | Free space or pick another container. |
| `USER_TAKEOVER` / `PLAYER_BUSY_MANUAL_ACTION` | You queued an action, or the mod saw one it did not queue. | Nothing. This is the design; automation waits for you. |
| `THREAT_INTERRUPTED` | The reflex guard stopped the action. | Deal with the zombie. |
| `PANIC_STOP` | The panic latch was set. | Re-arm deliberately when ready. |
| `GAME_DISCONNECTED` / `STALE_SESSION` / `SESSION_TERMINATED` / `SAVE_CHANGED` | The session ended, the link went stale, the character died, or the save changed. | Restart and re-arm. Nothing re-arms itself. |
| `SEQ_CONFLICT` | A sequence gap or a duplicate the journal could not reconcile. | Re-observe; the sidecar requests a full snapshot on a gap. |
| `INTERNAL_ERROR` | A bug. | `pz-agent logs --bundle --verify` and file it. |

Only five codes are retryable — `PATH_STUCK`, `NO_PROGRESS`, `ACTION_TIMEOUT`,
`QUEUE_REJECTED`, `PLAYER_BUSY_MANUAL_ACTION` — and the `retryable` field of an
MCP error payload tells you which one you are holding, so you do not have to
keep your own table.

---

## "Nothing happens"

This has four distinct causes that look identical from the outside. `doctor`
tells them apart; the ordering below is by frequency.

**The mod is installed but not enabled.** Installing copies files; it does not
tick the box. Mods menu → enable **PZ Agent Bridge** → *reload the save*.
Enabling a mod does not affect an already-loaded game.

**No save is loaded.** The mod writes a heartbeat only once a character exists.
A game sitting on the main menu produces no heartbeat, which looks exactly like
a mod that failed to load.

**The session is in `OBSERVE`.** The default. It reads and does nothing until
you arm it — `pz-agent arm --mode assisted`.

**The agent is armed but the player is busy.** If you have a timed action
running, automation waits rather than fighting you for the queue. `pz-agent
status` shows the action ownership.

## "Game not found"

The scanner walks **all** Steam libraries via `libraryfolders.vdf`, not just the
default one. If it still misses your install:

- Check the `searched` list in `doctor --json` — it reports every location it
  tried, which usually makes the wrong assumption obvious.
- A non-Steam install (GOG, a manual copy) is not discovered. Point at it
  explicitly in configuration.
- A drive that was offline when Steam last wrote `libraryfolders.vdf` will not
  appear in it.

## "Build could not be determined"

The version metadata file was missing or unreadable. The agent reports this
rather than guessing `42.20`, because a wrong build assumption silently
invalidates every capability probe.

You can proceed — every capability drops to `available_unverified`, so nothing
claims to be verified when it is not. Actions still work if the APIs are there;
they are just not certified.

## "Capability unsupported"

Working as intended. The probe did not confirm that API against your build, so
the matching action is honestly unavailable instead of being offered and then
failing halfway through.

`autonomous_attack` is permanently `unsupported` — there is no verified API for
it and there will not be one here.

If a capability you expect is unsupported after a game update, re-run
`pz-agent doctor` to rebuild the report. A report from a previous build has
every `verified` entry downgraded on load, by design.

## "POSTCONDITION_FAILED"

The command ran and the mod may even have reported success, but the observed
world did not change the way the action promised. Hunger did not drop; the item
is not in the destination container; the character is not within the target
radius.

**This is the system working.** A screen-scraping bot would have reported
success. Look at the `evidence` in the result — it contains the before and after
values that failed to move.

Common real causes: the item was consumed by something else first; the container
was full; the transfer was interrupted; the character was interrupted mid-action
by something the reflex guard did not classify as a threat.

## "NO_SAFE_FOOD" with a full backpack

The food policy rejected every candidate, and it will tell you why each one
lost — raw, rotten, burnt past the threshold, poisonous, tainted, needs a tool
you do not have, marked as your reserve, or the last strategic item while hunger
is not yet critical.

The rejection list travels with the refusal itself: the planner returns one
line per candidate it turned down, so it reaches you through whatever asked —
the MCP `plan` tool's response, or the voice adapter's answer. `pz-agent status`
reports the session, not a decision it did not make, and has no flag for this.

If you disagree with a rule, the one you can change without editing code is the
reserve: `pz-agent remember list` shows what is currently held back and
`pz-agent remember release <item>` stops reserving it. The freshness and
rot thresholds are policy in `pz_agent_core/policy/food.py` and are not exposed
in `config.toml` — `[safety]` holds `allow_multiplayer`,
`disabled_capabilities`, `manual_takeover`, `max_autonomous_radius` and
`panic_hotkey`, and nothing else. None of the five reaches a selection rule.
`disabled_capabilities` comes closest and only subtracts: its accepted names are
read from the probe table, so it can switch a capability off and can never
invent one, and a name that is not a probe's is a configuration error rather
than an ignored line.

## "INVALID_REF"

A reference from a previous session or a previous save generation. Every item,
container and square reference is scoped to a session and bumped by a save/load
transition, so a plan built before the transition cannot be resumed against
stale references.

Re-observe and rebuild. This is not retryable, and retrying is why the code
refuses to.

## "LEASE_EXPIRED"

The command's TTL ran out before it could execute — usually because it queued
behind a long timed action. Expiry is checked again immediately before
execution, on purpose: acting on a ten-second-old intention against a world that
has moved is worse than not acting.

If you see this constantly, the lease is too short for the actions you are
running, or the game is paused.

## "STALE_SESSION" / heartbeat lost

One side stopped writing its heartbeat.

- **Game heartbeat gone** → the game closed, crashed, or the mod was disabled.
  In-flight actions close as `lost`.
- **Sidecar heartbeat gone** → the sidecar died. The mod starts no new action.

Neither side re-arms itself on recovery. Restart the sidecar with
`pz-agent start`, then arm again deliberately.

## "play said it armed, but the agent does nothing"

Two different faults print almost the same thing, and the difference is which
session the evidence belongs to.

- **`play` refuses with "the game's heartbeat belongs to session … , not to the
  session this sidecar attached"** → the game is still running against a save an
  earlier sidecar attached to. Nothing was armed. Stop the game, or run
  `pz-agent stop` and start again, so both sides agree on one session. This
  refusal is the correct answer, not a bug: an arm confirmed by a previous
  session's heartbeat would be authority nobody granted.
- **`pz-agent status` shows the game attached and armed, but no action ever
  runs** → check the sidecar's diagnostics for a refused snapshot naming another
  session. A sidecar that attaches into an exchange directory still holding the
  previous session's observation slots reports that it has no picture of the
  world rather than acting on a dead one, and it clears as soon as the mod
  publishes under the live session. If it does not clear, the mod is not
  publishing: see `PZD006`.

## "The goal stopped without saying anything"

It should not, and if you see it the report is worth filing. Every goal ends by
a named reason: a step whose outcome could never be observed — the action's
record aged out of the channel's history, or the channel was replaced — ends the
goal `CAPABILITY_UNAVAILABLE` on the next tick rather than leaving it running
against a step nobody can settle. A goal that sits in progress with nothing
being dispatched is a defect, not a slow plan.

## "Restore refused"

`restore-save` will not run while Project Zomboid is open. This is an exception, not
a warning, and there is no flag to override it — restoring a save under a
running game is how you get a corrupted one.

Two things are checked, in this order:

1. **The game heartbeat.** A fresh one proves the game is open. Its absence
   proves nothing on its own — a game sitting on the main menu, or one whose
   mod is disabled, writes none either.
2. **The process table** (`tasklist` on Windows, `ps` elsewhere). Only a listing
   that actually ran may be read as "the game is closed".

So `restore-save` also refuses when it could not read the process table at all
(`ps` missing, the command timed out, the listing was truncated). The message
names which of these happened. That is deliberate: "we could not tell" is not
permission.

Close the game and retry.

## Duplicate actions / "it ate twice"

It should not, and this is worth reporting. Redelivery is supposed to be safe:
a command whose idempotency key already reached a terminal result gets that
original result replayed rather than a second execution.

The one real edge is a cache eviction — the idempotency cache is bounded, so a
duplicate whose key was evicted long ago would be treated as new. Attach
`pz-agent logs --bundle --verify` and the two command ids.

## The agent keeps trying the same thing

Anti-loop protection should catch this: a need that re-triggers without its
underlying state improving is rate-limited over a bounded window and then
escalated to you.

If it is thrashing anyway, `pz-agent stop`, then attach the trace. A loop that
gets past the rate limiter is a bug in the arbitration, not a tuning problem.

## Unicode paths

A Cyrillic (or any non-ASCII) Windows username is a supported, tested case. If a
path-related failure mentions encoding, that is a bug — include the *redacted*
`doctor --json` output, which replaces your home directory with a placeholder.

## Unsigned binary warning

Expected. The packaged launcher is not code-signed; signing is not part of this
project. Documented rather than worked around.

---

## Filing a useful report

```powershell
.venv\Scripts\pz-agent logs --bundle --verify
```

`--verify` prints exactly what the archive contains after redaction — absolute
paths replaced, secrets stripped. Check it before attaching.

Include: the build, the mode the agent was in, the reason code, and the last
few lines of the action trace. The reason code alone usually identifies the
subsystem.
