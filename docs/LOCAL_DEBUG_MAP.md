# Local debug map

Symptom → the module most likely at fault → the log that settles it → what to do.

This is a triage table, not a troubleshooting guide. `docs/TROUBLESHOOTING.md` is
for a user whose install misbehaves. This file is for the agent running live
tests, who has a failing scenario and needs to know *where to look first* rather
than which twenty things it might be.

Read it with one assumption in mind: **almost every failure in a first live run
is a Build 42.20 API signature that differs from what the adapter declared.**
The mod was written without a game to check against, every engine symbol is
probed rather than called blind, and every guessed signature carries a
`-- Build 42:` comment. So when something does not work, the first question is
not "is the design wrong" — it is "which symbol is spelled differently here".

```
grep -rn "Build 42:" pz-mod/
```

That finds a handful of comments and is a shortcut, not an inventory. The full
list of what is unconfirmed is `docs/GAME_API_VERIFICATION.md`, which states its
own size and is the only document that does — check there before concluding a
symbol is fine because the grep did not name it.

That is the list of every place this code is guessing.

## Where the evidence lives

Nine places answer nearly every question. Paths are relative to the exchange
directory, which `pz-agent status` prints.

| What | Path | Written by |
|---|---|---|
| Mod → sidecar observations | `observation.events.0001.jsonl` | mod |
| Latest full snapshot | `observation.snapshot.a.json` / `.b.json`, current slot named in `observation.snapshot.pointer` | mod |
| Sidecar → mod commands | `command.queue.0001.jsonl` | sidecar |
| Mod → sidecar acks | `command.ack.0001.jsonl` | mod |
| Mod liveness | `heartbeat.game.json` | mod |
| Sidecar liveness | `heartbeat.sidecar.json` | sidecar |
| Capability report | `capabilities.json` | mod |
| Panic latch | `panic.stop` | either |
| Sidecar diagnostics | `logs/` | sidecar |

The game's own log is `%USERPROFILE%\Zomboid\console.txt`. Every Lua error the
mod raises lands there and nowhere else — the sidecar cannot see a Lua error,
because a mod that has crashed is a mod that has stopped writing.

The pointer file is the one non-obvious piece: snapshots alternate between two
slots and the pointer is written **last**, so a reader that trusts the pointer
never sees a half-written snapshot. If you find yourself reading a snapshot file
directly, read the pointer first.

## The table

### Nothing at all is happening

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| No `heartbeat.game.json` ever appears | `Heartbeat.lua`, `Ipc.lua`, `mod.info` | `console.txt`, exchange dir listing | The mod did not load. Check `console.txt` for `PZAgent` at startup and for a Lua error during load. Confirm the mod is enabled in the in-game mod list, not merely present on disk. |
| `heartbeat.game.json` exists but is stale | `Heartbeat.lua`, `Runtime.lua` | `heartbeat.game.json` timestamp vs now | The mod loaded and then stopped ticking — almost always a Lua error thrown inside the tick. `console.txt` has it. |
| `pz-agent status` says the mod is absent while the game is open | `Ipc.lua`, exchange dir resolution | `pz-agent status --json`, `pz-agent doctor` | The two sides disagree about *which directory* is the exchange. `doctor` prints both. |
| Sidecar exits immediately on `start` | `pz_agent_cli/supervisor.py`, `runtime.py` | `logs/`, stderr | Usually a config or lock problem. `sidecar.lock` held by a dead process is the common one. |

### The command goes nowhere

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| Command written, no ack of any kind | `CommandReader.lua` | `command.queue.0001.jsonl`, `console.txt` | The reader never saw it or died parsing it. Check the last line of the queue is complete JSON with a trailing newline; a partial line is held deliberately, not skipped. |
| Ack says `rejected`, reason `INVALID_ARGUMENT` | `CommandDispatcher.lua` + the adapter's `args` declaration | `command.ack.0001.jsonl` (the detail names the argument) | The declaration and the sender disagree. The dispatcher refuses undeclared keys on purpose — a silently dropped `radius` runs the action with a default nobody asked for. |
| Ack says `rejected`, reason `INVALID_REF` | `Refs.lua`, `Toolkit.resolveItem`/`resolveContainer` | ack detail, latest snapshot | Either the ref is from a previous session (its runtime ids denote different objects now — re-observe, do not retry) or the object is gone. |
| Ack says `rejected`, reason `SEQ_CONFLICT` or a duplicate replay | `CommandReader.lua`, `ActionRuntime.lua` | `command.queue.0001.jsonl`, `command.ack.0001.jsonl` | Sequence numbers must rise. A replayed idempotency key returns the recorded ack by design — that is not a bug unless the key was reused for different arguments. |
| Ack says `rejected`, reason `LEASE_EXPIRED` before anything started | `CommandReader.lua` lease check, `Safety.leaseExpired` | ack detail, command `issued_at_ms` + `lease_ms` | The command's lease had already run out when the mod read it. Look at the gap between the sidecar's write and the mod's read: a long gap means the mod's tick is starved. |
| Ack says `rejected`, session id mismatch | `Session.lua` | `session.json`, ack detail | The sidecar restarted and minted a new session, or the mod did. Re-arm. |

### Accepted, but the character does not move

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| Status stays `accepted`, never `started` | the adapter's `start()`, `CapabilityRuntime.lua` | `capabilities.json`, `console.txt` | A required engine symbol is missing. The ack detail names it — that is the whole point of naming symbols in `required_symbols`. Verify the class exists in this build. |
| `CAPABILITY_UNAVAILABLE` naming a class | `Toolkit.construct`, the adapter's `required_symbols` | ack detail | **The most likely failure of a first live run.** The class was renamed or moved in Build 42. Find the real name in the game's Lua, fix `required_symbols` and the construct call, re-run. |
| Status `started`, never `progress`, queue empty | `Toolkit.enqueue`, `ISTimedActionQueue` | `console.txt`, action journal | The timed action was constructed but the queue refused it, or the constructor's argument order differs from what the adapter passed. Constructor arity is the classic Build 42 break. |
| Status `progress` forever, character visibly stuck | the movement adapter's stall check | ack `progress` entries, snapshot positions | `Toolkit.trackProgress` should turn a distance that stops falling into `PATH_STUCK`. If it does not, the adapter is tracking the wrong measure. |
| `PLAYER_BUSY_MANUAL_ACTION` | `Ownership.lua`, `Safety.describeQueue` | ack detail, `console.txt` | The queue holds work the mod does not own. Either the player queued something, or our own action lost its ownership tag — check that `Toolkit.enqueue` stamped it. |

### It acted, but the result says it failed

This block matters more than the rest, because it is where an honest system and
a lying one differ. A `failed` here usually means the action *worked* and the
verifier could not prove it.

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| Ate the food, result `failed` | the postcondition verifier for `consume.eat` | before/after snapshots in the ack evidence | The stat getter is spelled differently in this build. Find the real `Stats` accessor, fix the reader, re-run. Do not relax the postcondition. |
| Transferred the item, result `failed` | `inventory.transfer` postcondition | ack evidence: source and destination contents | The check requires the item in the destination **and gone from the source**. If the source read is failing, the transfer looks like a copy. Fix the source read. |
| Bandaged, result `failed` | `Medical.lua` body-part accessor | ack evidence `bleeding_before`/`bleeding_after`, `bandaged_after` | The body-part enumeration or the bandaged flag is named differently. |
| Read the book, result `failed` | `Literature.lua` page counter | ack evidence `pages_before`/`pages_after` | The page counter may live on the reading action rather than the item in this build. |
| Slept, result `failed` | `Sleep.lua` asleep flag + fatigue | ack evidence `fatigue_before`/`fatigue_after` | Confirm both measures — sleep must show the character observed asleep and fatigue falling, not just one of the two. |

**Never fix one of these by weakening the postcondition.** A postcondition that
passes without observing anything turns every subsequent scenario's PASS into a
guess. Fix the reader.

### Safety and lifecycle

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| Panic stop does not stop anything | `Safety.lua`, `Ownership.lua` | `panic.stop`, `console.txt`, ack journal | Check the latch file appears, then check `describeQueue` can read the queue at all. An unreadable queue must not be reported as "nothing to cancel". |
| Panic stop cancelled the player's own action | `Ownership.lua` | ack evidence: cancelled vs left | The ownership tag is being applied too broadly, or read too loosely. This is a safety defect — fix before continuing the run. |
| Manual takeover not detected | `Safety.lua` input hooks | `console.txt`, snapshot `safety.manual_takeover` | The player-input event names differ in this build. This is the one place where a wrong symbol is dangerous rather than merely broken. |
| Sidecar died, mod kept acting | `Safety.lua` staleness check (`sidecarStale`), `ActionRuntime.lua` | `heartbeat.sidecar.json` timestamp, ack journal | Heartbeat-loss stop did not fire. Verify the mod compares against its own clock, not a timestamp the sidecar wrote. |
| Mod kept acting after `disarm` | `Session.lua`, `ActionRuntime.lua` | ack journal, `session.json` | A disarm arriving during a long action must end that action. If the disarm was recorded but the action continued, the runtime is re-checking arm state only at start. |
| Restart lost track of an in-flight action | `ActionRuntime.lua` recovery, `pz_agent_cli/runtime.py` | ack journal, `session.json` | On restart neither side may assume an action completed. An action whose outcome is unknown is unknown, not failed and not succeeded. |

### Voice and planning

| Symptom | Likely module | Log to read | What to do |
|---|---|---|---|
| Russian phrase not recognised | `pz_agent_voice/intent.py`, `phrases.py` | `logs/` | Add the phrasing to the intent table with a test. Do not loosen the matcher — a fuzzy match on "стоп" is a stop that fires on the wrong word. |
| "Стоп" heard but the character kept going | `pz_agent_voice/driver.py` interim-transcript path | `logs/`, ack journal | Stop must fire on the **interim** transcript, before the final one. If it waited for the final, the latency budget is blown. |
| Planner proposed an impossible step | `planner/critic.py`, capability validation | `logs/`, `capabilities.json` | The critic should have refused it. A model proposing nonsense is expected; the critic letting it through is the defect. |
| Provider returns a plan that will not parse | `planner/providers/*.py` | `logs/` | Correct behaviour is a typed rejection carrying the parse fault. If it raised instead, fix the provider, not the model. |

## Measuring the latency targets

`pz-agent latency` reads the command and ack journals (rotated generations
included), the observation stream, the snapshot pointer and the heartbeat
files, joins commands to their acks by `command_id`, and prints exact
nearest-rank percentiles (count/min/p50/p95/max) for submit→accepted,
accepted→started, started→terminal, submit→terminal, the safety-stop reaction
and the observation interval. `pz-agent latency --json` emits the raw report,
and `pz-agent latency --targets` compares it against the P0 targets
(submit→accepted p95 ≤ 250 ms, terminal ack visibility ≤ 250 ms, observations
≥ 4 Hz, safety reaction ≤ 200 ms), marking each MET, MISSED or UNMEASURED. It
exits non-zero only on a *measured miss* under `--targets`: an UNMEASURED
target exits 0, deliberately, so a CI machine with no game does not fail for
lacking one. An interval nothing on disk records is reported as unmeasured,
never estimated.

One caveat to read the numbers with: `issued_at_ms` is stamped by the
sidecar's wall clock and every ack stamp is the game process's, and nothing
anywhere corrects the skew between the two — so the submit→accepted and
submit→terminal rows are labelled cross-clock, include that skew, and can even
be negative. The single-clock rows (accepted→started, started→terminal, the
observation interval) are the trustworthy ones on a machine with drifting
clocks. Live p95 numbers for the targets are therefore the game machine's to
produce, from its own exchange directory during a real session; nothing in
this repository can honestly invent them.

## What to send when you are stuck

`pz-agent logs --bundle` builds a redacted archive. Send that, plus:

- `console.txt` (the whole file, not an excerpt — the load-time errors are at the top)
- the failing scenario's `evidence/S<nn>_*/` directory
- the output of `pz-agent doctor --json`
- the exact game build from the main menu

Do not hand-edit any of it. A redacted bundle is designed to be safe to share;
an edited one is evidence of nothing.
