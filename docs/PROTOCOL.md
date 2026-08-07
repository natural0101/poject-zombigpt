# Protocol

The Lua mod and the sidecar share no memory, no socket and no process. They
communicate through a set of files in the user's Lua directory, written as a
**journal of commands and acknowledgements** rather than as a shared mutable
blob.

Source of truth: the JSON Schema documents in [`schemas/`](../schemas). The
Python dataclasses in `pz_agent_core.protocol` are the in-process mirror, and
[`tests/contract/`](../tests/contract) fails the build if the two disagree.

---

## Why files

Sockets from inside a Kahlua mod are restricted; writes to the user's own Lua
directory are supported and observable. Files also give the protocol two
properties that matter more than latency:

- **Crash tolerance.** Either side can die at any moment. The survivor reads
  the journal, sees where it got to, and resumes — nothing is lost in a socket
  buffer that no longer exists.
- **Inspectability.** When something goes wrong, the entire conversation is on
  disk in plain text. `pz-agent logs` and `pz-agent replay` read the same files
  the agent did.

The cost is polling latency, which is acceptable: a timed action in Project
Zomboid takes seconds, not milliseconds.

## Directory

```
%USERPROFILE%\Zomboid\Lua\pz_agent\
├── session.json                    handshake, written by the sidecar
├── capabilities.json               probe results, written by the mod
├── observation.snapshot.a.json     full snapshot, slot A
├── observation.snapshot.b.json     full snapshot, slot B
├── observation.snapshot.pointer    names the slot that is complete
├── observation.events.0001.jsonl   diff stream
├── command.queue.0001.jsonl        sidecar → mod
├── command.ack.0001.jsonl          mod → sidecar
├── heartbeat.game.json             written by the mod
├── heartbeat.sidecar.json          written by the sidecar
├── panic.stop                      presence means stop; content is irrelevant
└── logs/
```

**Every filename is a hardcoded constant on both sides.** A command cannot name
a file, and no code path joins user input into a path under this directory.
That is what stops "write this observation to `../../../autoexec`" from being a
sentence anyone can say.

`panic.stop` is a file rather than a message on purpose: creating it requires no
working protocol, no live session and no agreement about sequence numbers. If
everything else is broken, `panic.stop` still stops the agent.

---

## Sessions

The sidecar writes `session.json`:

```json
{
  "protocol_version": "1.0",
  "session_id": "d7c1f0a2-…",
  "created_at_ms": 1700000000000,
  "sidecar_version": "0.1.0",
  "requested_observation_hz": 4,
  "mode": "observe",
  "nonce": "b41f…"
}
```

The mod accepts it only when **all** of these hold:

1. `session_id` is a well-formed UUID.
2. The protocol **major** version matches. Minor versions are additive by
   contract, so `1.7` talks to `1.0`; `2.0` is refused.
3. `created_at_ms` is recent — a session file left over from last week is not a
   request to start now.
4. The `nonce` differs from the previous session's. A repeated nonce means a
   stale file was replayed, not that a new sidecar attached.
5. The sidecar heartbeat is alive.

The mod replies in `heartbeat.game.json` with the same session id and a nonce of
its own. Both sides now know they are talking to a live, current peer.

Sessions start in `OBSERVE`. Arming is a separate, explicit command.

## Sequences

Four independent monotonic counters: observation, command, ack, event.

A **gap** is never interpolated. When the sidecar sees one it requests a full
snapshot; when the mod sees one it does not attempt to guess the missing
command. Guessing here means executing something the user did not ask for.

Duplicates are recognised by `command_id` and `idempotency_key`. A command whose
key has already reached a terminal result gets that **original result replayed** —
it is not executed a second time. This is what makes at-least-once delivery
safe over a file journal.

## Writing without atomic rename

Atomic rename is not guaranteed from inside Kahlua, so full snapshots use two
alternating slots and a pointer:

1. Write the whole document to slot A (or B — whichever is not current).
2. Flush and close.
3. Overwrite the pointer file with the slot name.

The pointer is written last and is small enough that a torn write is
detectable. A reader that finds an invalid document follows the other slot,
which still holds the previous good snapshot. The worst case is one stale
snapshot, never a half-parsed one.

Journals (`.jsonl`) use a different discipline:

- One record per line, newline-terminated.
- A reader tracks a **byte offset**. A trailing line with no newline yet is
  ignored and re-read next tick — the writer is still mid-write.
- A complete but unparseable line is reported as a corrupt record and skipped,
  so one bad line cannot stall the stream forever.
- Files are size-capped and rotate; rotation is signalled to the reader rather
  than silently losing records.

## Observation tiers

| Tier | Content | Who produces it | When |
| --- | --- | --- | --- |
| 0 | Heartbeat: session, seq, versions, player present, armed, mode, active action, danger | The mod | Every tick |
| 2 | Full snapshot | The mod | Every observation interval, on connect, after a gap, after recovery |
| 1 | Compact diff: changed scalars and changed ref lists | **The sidecar**, from two consecutive snapshots | On demand, for the planner and the trace |
| 3 | Requested detail: one container's contents, a wound, a square, a candidate path | The mod | On demand |

**Tier 1 is not a wire format.** The mod writes full snapshots; the sidecar
derives the diff by comparing two of them (`observation/diff.py`). There is no
diff schema in `schemas/`, and a diff is never parsed off the exchange
directory.

That is a deliberate trade. A diff on the wire would be smaller, but it would
make the mod hold the previous snapshot and stay in step with a reader it
cannot see — and a reader that missed one line would then be silently wrong
rather than noisily behind. Full snapshots are idempotent: a reader that misses
one loses a tick, not its grip on the world.

Applying a tier-1 diff to the previous snapshot still has to reproduce the next
one exactly, and that round-trip is a test rather than an aspiration — it is
what lets the trace be replayed and the planner be given a compact update.

### Saying where the walk stopped

A snapshot is built by walking things the player controls the size of — a
hoarder's inventory, a horde, a warehouse of shelves — so every walk in the mod
is capped. A capped walk that says nothing is worse than no walk at all: an
inventory list that stops at 512 items reads exactly like an inventory that
holds 512 items, and a planner cannot tell "there are no zombies" from "we
stopped counting".

Every object in `observation.schema.json` is `additionalProperties: false`
except item and nearby entries, so there is no dedicated slot for this.
`player.stats` is the one open scalar map, and the mod reports its own limits
there under the reserved `observe.` prefix:

| Key | Meaning |
| --- | --- |
| `observe.<section>_truncated` | A cap bit while walking that section |
| `observe.<section>_omitted` | How many entries were left behind |
| `observe.chasing_unknown`, `observe.visible_unknown` | A zombie flag this build would not answer |
| `observe.paused_unknown`, `observe.speed_unknown` | A clock reading that fell back rather than being read |

`<section>` is one of `containers`, `items`, `stats`, `objects`, `zombies`,
`wounds`, `moodles`. The keys are written **only when they are true**, so
silence on a section that is present means that section is complete. The mod
refuses any game stat whose name starts with `observe.`, because a stat that
could overwrite one of these could hide exactly the fact it is reporting.

## Stable references

The LLM never receives a Lua table or a Java handle. It receives an opaque
string the mod can re-resolve and re-validate immediately before acting:

```
item:<session>:<container-tail>:<runtime-id>:<generation>
container:<session>:player-main
container:<session>:worn:<slot>:<item-runtime-id>
container:<session>:world:<x>:<y>:<z>:<object-index>:<container-index>
square:<session>:<x>:<y>:<z>
zombie:<session>:<runtime-id>:<generation>
```

Two details that are easy to get wrong and expensive when you do:

**A container reference contains colons of its own.** A world container tail is
five colon-separated numbers. Parsing an item reference with a naive
`split(":")` therefore does not fail loudly — it produces a *different valid
reference*, pointing at some other object. Both the Python and the Lua
implementations parse from the ends (`partition` from the left for the kind and
session, `rpartition` from the right for the generation and runtime id) and
treat everything between as the opaque container tail.

**Generation.** Anything that can invalidate object identity — a save/load
transition, a new session — bumps the generation. A reference minted before the
transition then fails validation instead of resolving to whatever now occupies
that runtime id. References are also session-scoped: one from a previous
session is `INVALID_REF`, not a retryable miss.

## Capabilities

`capabilities.json` records **probe results**, not intentions:

```json
{
  "build": "42.20",
  "protocol_version": "1.0",
  "capabilities": {
    "move_to_square":      {"state": "verified"},
    "inventory_transfer":  {"state": "verified"},
    "eat_percentage":      {"state": "verified"},
    "drink_world_source":  {"state": "experimental"},
    "autonomous_attack":   {"state": "unsupported", "reason": "NO_VERIFIED_API"}
  }
}
```

| State | Meaning |
| --- | --- |
| `verified` | A probe ran against the live game and confirmed it |
| `available_unverified` | The symbol exists in the local files; nothing has exercised it |
| `experimental` | Works, but not reliably enough to use unattended |
| `unsupported` | No verified API. The reason is recorded |
| `disabled_by_policy` | Available, and listed in `safety.disabled_capabilities` |

A static scan of local Lua files can produce `available_unverified` at best.
Only a live runtime confirmation produces `verified`, and a report loaded from a
different build downgrades every `verified` entry — a report from 42.19 proves
nothing about 42.20.

The MCP boundary does not publish a write tool as ready when its capability is
`unsupported` or `experimental`.

## Commands

```json
{
  "protocol_version": "1.0",
  "session_id": "d7c1f0a2-…",
  "seq": 12,
  "command_id": "9b2e…",
  "idempotency_key": "goal-1:step-2:attempt-1",
  "issued_at_ms": 1700000000000,
  "lease_ms": 10000,
  "expected_observation_seq": 55,
  "action": "consume.eat",
  "args": {"item_ref": "item:…", "fraction": 0.5},
  "policy": {"allow_interrupt": true, "max_retries": 1}
}
```

The `action` field is a **closed enum**. The mod rejects anything outside it
before dispatch, which means a new action cannot be introduced by a well-crafted
payload — only by changing the schema, the mod's dispatch table and the
capability probe together.

`lease_ms` is a time-to-live. Expiry is checked twice: on receipt, and again
immediately before execution. The second check is the one that matters — a
command can sit behind a long timed action while the world moves on, and
executing it late is worse than not executing it.

## Acknowledgements

```
received → accepted → started → progress* → succeeded
                    ↘ rejected            ↘ failed / cancelled / lost
```

Terminal: `succeeded`, `failed`, `cancelled`, `rejected`, `lost`.

**`succeeded` is a claim about the world.** It means a postcondition was
observed — hunger dropped, the item is in the destination container, the
character is within the target radius. It never means "the command was queued";
that is `accepted`.

This is enforced structurally rather than by convention:

```python
ActionResult.succeeded(..., evidence={"hunger_before": 0.72, "hunger_after": 0.31})
```

`succeeded()` raises without evidence, and constructing an `ActionResult` with
`status="succeeded"` and any reason other than `POSTCONDITION_MET` raises too.
And in the action engine, if the adapter's `verify` never returns evidence the
result is `POSTCONDITION_FAILED` **even when the mod acked `succeeded`** — the
mod's opinion does not override observation.

### Reason codes

Stable and append-only. Renaming one is a protocol major bump.

| Group | Codes |
| --- | --- |
| Success | `POSTCONDITION_MET` |
| Session | `NOT_ARMED` `STALE_SESSION` `LEASE_EXPIRED` `SEQ_CONFLICT` `GAME_DISCONNECTED` `SAVE_CHANGED` `SESSION_TERMINATED` |
| Validation | `CAPABILITY_UNAVAILABLE` `INVALID_REF` `INVALID_ARGUMENT` `PRECONDITION_FAILED` `QUEUE_REJECTED` `POLICY_DENIED` |
| Interruption | `PLAYER_BUSY_MANUAL_ACTION` `USER_TAKEOVER` `THREAT_INTERRUPTED` `PANIC_STOP` `CANCELLED_BY_REQUEST` |
| Movement | `PATH_NOT_FOUND` `PATH_STUCK` `TARGET_OUT_OF_RANGE` `TARGET_NOT_LOADED` |
| Verification | `ACTION_TIMEOUT` `POSTCONDITION_FAILED` `NO_PROGRESS` |
| Domain | `NO_SAFE_FOOD` `NO_SAFE_DRINK` `NO_SUITABLE_LITERATURE` `CONTAINER_FULL` `RESOURCE_RESERVED` |
| Catch-all | `INTERNAL_ERROR` |

Only `RETRYABLE_CODES` may be retried, and only within the command's retry
budget. Retrying an `INVALID_REF` or a `POLICY_DENIED` just burns the budget.
`PLAN_FATAL_CODES` stop the whole plan, not merely the current step.

## Backpressure

- At most **one mutating command in flight**. The mod does not accept a second.
- The sidecar may hold a plan, but sends the next step only after a terminal ack.
- `safety.stop` bypasses the queue entirely.
- Observation events may coalesce under load. Action results and safety events
  never do — dropping those is how a user loses track of what the agent did.

## Recovery

| Event | Consequence |
| --- | --- |
| Sidecar restarts | Reads the session, mints a new nonce, does **not** re-execute commands, learns the active action, then monitors or cancels per policy |
| Game restarts | New session generation; every ref is invalid; in-flight commands close as `lost`; re-arming is required |
| Save changed | `save_id` differs → world refs are dropped, preferences survive, autonomous mode does **not** resume by itself |
| Heartbeat lost | Sidecar heartbeat gone → no new task starts. Game heartbeat gone → in-flight actions close as `lost` |

Nothing in this table re-arms the agent automatically. Coming back from a crash
into an armed autonomous state is exactly the surprise this design refuses.

## Privacy

The protocol does not carry the Windows username, absolute paths, Steam tokens,
chat text, the process list, or the contents of arbitrary files. `save_id` is a
truncated hash rather than a path. Diagnostic bundles redact paths and secrets
before they are written, not on the way out.

See [`PRIVACY.md`](../PRIVACY.md) for what an external LLM provider would see if
you enable one, and [`SAFETY.md`](SAFETY.md) for the enforcement model.
