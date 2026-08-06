# Troubleshooting

Start here:

```powershell
.venv\Scripts\pz-agent doctor
```

Every check has a stable code and remediation text. The sections below explain
the failures whose cause is not obvious from the message.

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
