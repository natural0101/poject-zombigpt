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
| `PZD006` | `game_heartbeat` | No `heartbeat.game.json`, or one whose timestamp is not advancing. This is the check that distinguishes "the mod is installed" from "the mod is running": a mod that threw during load looks identical to an idle exchange directory everywhere except here. Enable **PZ Agent Bridge** in the in-game mod list and load a save. |
| `PZD007` | `ipc_writable` | The exchange directory could not be written to, so the sidecar could not send a command even if everything else were healthy. |
| `PZD008` | `timed_actions` | The timed-action classes the adapters construct could not be found by a scan of the install's own Lua. This is what turns capabilities from unprobed into a state backed by evidence; a failure here explains a later `CAPABILITY_UNAVAILABLE`. |
| `PZD009` | `conflicting_files` | Something is left over in the exchange directory that this build did not write — usually a previous version's journals. |
| `PZD010` | `active_session` | Reports whether a session is open and which mode it is in. `unknown` with no exchange directory is normal before the first run. |

A check can report `pass`, `warn`, `fail` or `unknown`, and **`unknown` is not a
pass**. It means the check could not be performed — almost always because
something above it failed — and `doctor` says which.

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

`pz-agent status --explain` prints the rejection list. If you disagree with a
rule, the thresholds are in configuration; the reserve rules in particular are
meant to be tuned.

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

## "Restore refused"

`restore` will not run while Project Zomboid is open. This is an exception, not
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
