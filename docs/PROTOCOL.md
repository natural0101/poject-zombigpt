# Protocol

There are two wire boundaries in this system, and they are versioned apart.

| Boundary | Between | Version constant | Value on this build |
| --- | --- | --- | --- |
| Exchange protocol | Lua mod ↔ sidecar | `PROTOCOL_VERSION` | `1.1` |
| Payload schema | stamped into every observation, ack, event and plan | `SCHEMA_VERSION` | `1.0` |
| Local Core RPC | sidecar ↔ a local client process (today `pz-agent-mcp`) | `RPC_PROTOCOL_VERSION` | `1.0` |

All three live in `pz_agent_core/version.py`, which is the single source of
truth; `scripts/check_versions.py` fails the release when a restatement drifts.

The mod and the sidecar share no memory, no socket and no process. They
communicate through a set of files in the user's Lua directory, written as a
**journal of commands and acknowledgements** rather than as a shared mutable
blob.

---

## What is actually in `schemas/`

Eight files, and this section describes what each one contains rather than what
a reader might expect it to. All eight declare
`$schema: https://json-schema.org/draft/2020-12/schema` and an `$id` under
`https://example.local/pz-agent/`.

| File | Title | Top-level `required` | Closed? |
| --- | --- | --- | --- |
| `command.schema.json` | PZ Agent Command | `protocol_version`, `session_id`, `seq`, `command_id`, `idempotency_key`, `issued_at_ms`, `lease_ms`, `action`, `args` | yes |
| `action_result.schema.json` | PZ Agent Action Result | `schema_version`, `session_id`, `seq`, `command_id`, `action`, `status`, `reason_code`, `timestamp_ms` | yes |
| `observation.schema.json` | Project Zomboid Agent Observation | `schema_version`, `session_id`, `seq`, `timestamp_ms`, `game`, `player`, `safety`, `action` | mostly — see below |
| `event.schema.json` | PZ Agent Event | `schema_version`, `session_id`, `seq`, `timestamp_ms`, `type`, `data` | yes |
| `plan.schema.json` | PZ Agent Typed Plan | `schema_version`, `goal_id`, `summary`, `risk_class`, `steps` | yes |
| `config.schema.json` | PZ Agent Configuration | `game`, `session`, `safety`, `planner`, `voice` | yes |
| `core_rpc_request.schema.json` | PZ Agent Local Core RPC Request | `format`, `protocol`, `id`, `method`, `params` | yes |
| `core_rpc_response.schema.json` | PZ Agent Local Core RPC Response | `format`, `protocol`, `id`, `ok` | yes |

Only three of the eight are exercised by a conformance test.
`tests/contract/test_schema_conformance.py` validates `command.schema`,
`action_result.schema` and `observation.schema` in both directions — what
`to_dict()` emits must validate, and what the schema permits must parse — and
also asserts that every `ActionName`, `ActionStatus`, `ReasonCode`,
`SessionMode`, `DangerLevel`, `ContainerKind`, `RiskClass`, `ActionOwnership`
and `CapabilityState` member appears where the schema names it.
`tests/contract/test_core_rpc_schema_conformance.py` covers the two Core RPC
documents. `plan.schema.json` is checked from a unit test
(`tests/unit/test_planner_plan.py`), which validates a built plan against it and
pins `MAX_PLAN_STEPS` and `MAX_SUMMARY_CHARS` to the schema's own numbers.

**`event.schema.json` and `config.schema.json` are validated by nothing in this
tree.** Nothing loads them at runtime and no test asserts against them.
`config.schema.json` has visibly drifted from `pz_agent_cli.config.SCHEMA`,
which is what actually validates `config.toml`:

| Point | `config.schema.json` says | `pz_agent_cli.config.SCHEMA` does |
| --- | --- | --- |
| `game` keys | `install_path`, `user_path`, `expected_build` | `channel`, `expected_build`, `install_dir`, `user_dir` |
| `session.default_mode` | `OFF`/`OBSERVE`/`ASSISTED`/`AUTONOMOUS`/`REFLEX_ONLY` | `observe`/`assisted`/`autonomous`, lower case |
| `planner.provider` | `none`/`openai`/`anthropic`/`local`/`teamon` | `none`/`openai_compatible`/`teamon` |
| `planner.max_steps` | maximum 10 | maximum 32 |
| `voice` keys | `enabled`, `adapter` | `enabled`, `adapter`, `api_key_env` |
| provider sub-tables | absent | `[planner.openai_compatible]`, `[planner.teamon]` |

Treat `pz_agent_cli.config.SCHEMA` as the configuration contract and
`config.schema.json` as stale. This document does not say which one *ought* to
win; it records that they disagree and that only one of them runs.

### Three different plan-step ceilings

Also worth knowing before writing a client, because all three are real and none
of them is a typo this document is entitled to correct:

| Where | Ceiling |
| --- | --- |
| `schemas/plan.schema.json` → `steps.maxItems`, and `pz_agent_core.planner.plan.MAX_PLAN_STEPS` | **5** |
| `pz_plan_execute`'s published `limits.max_steps` (`pz_agent_mcp.catalog.MAX_PLAN_STEPS`), which is also its default | **8** |
| `planner.max_steps` in `config.toml` (`pz_agent_cli.config.MAX_PLAN_STEPS`) | **32** |

`pz_agent_cli.autonomy.build_planner` clamps the configured value with
`min(config planner.max_steps, 5)` and says so in a comment. The MCP path's `8`
is *not* clamped anywhere this document could find, and
`PlanProposalRequest.__post_init__` raises `ValueError` for a `max_steps` above
5. What a client actually gets back from `pz_plan_execute` with the default
limits has **not been verified here** — it needs a running sidecar, which no
test in this tree stands up for that call.

---

## Why files

Sockets from inside a Kahlua mod are restricted; writes to the user's own Lua
directory are supported and observable. Files also give the protocol two
properties that matter more than latency:

- **Crash tolerance.** Either side can die at any moment. The survivor reads
  the journal, sees where it got to, and resumes.
- **Inspectability.** When something goes wrong, the entire conversation is on
  disk in plain text.

The cost is polling latency, which is acceptable: a timed action in Project
Zomboid takes seconds, not milliseconds.

## Directory

Every name below is a constant in `pz_agent_core/ipc/layout.py`.

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
├── sidecar.lock                    the sidecar's own mutual exclusion; the mod never reads it
└── logs/
```

`0001` is a **series** number, not a rotation counter: it changes only when the
journal changes shape, so an old reader stops cleanly instead of misparsing.
Rotation is expressed by `.1`/`.2` suffixes that `ipc/journal.py` appends.

**Every filename is a hardcoded constant on both sides.** A caller asks
`IpcLayout` for a *role* — `command_queue`, `panic_stop` — and receives a path
the layout composed itself. No value derived from a command, a plan or an
observation ever contributes to a path, and `IpcLayout.is_managed_path` exists
so a writer can assert that before opening a file.

`panic.stop` is a file rather than a message on purpose: creating it requires no
working protocol, no live session and no agreement about sequence numbers.

The Core RPC link lives elsewhere — under the sidecar's **state** directory, not
the exchange directory:

```
<state-dir>/runtime/
├── core-rpc.json    the descriptor: address, pid, protocol major, token file
└── <token file>     the shared secret for the link
```

---

## Commands

`schemas/command.schema.json`, `additionalProperties: false`.

```json
{
  "protocol_version": "1.1",
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

`protocol_version` is a `const` of `"1.1"`. `session_id` and `command_id` are
`format: uuid`. `idempotency_key` is 1–128 characters — note that the MCP
surface publishes a stricter 1–120. `lease_ms` is 100–300000.
`expected_observation_seq` is an integer or `null`. `policy` is a closed object
holding `allow_interrupt`, `max_retries` (0–3) and `risk_permission`
(`P0`…`P4`). `args` is an open object; what belongs in it is the adapter's
business, not the envelope's.

`action` is a **closed enum of 22 names**, and this is the whole list:

```
session.arm            session.disarm         safety.stop
action.wait            plan.cancel
movement.move_to       movement.move_near
world.inspect          container.inspect      container.open_nearby
inventory.search       inventory.transfer     inventory.ensure_main
consume.eat            consume.drink          consume.drink_source
literature.read
equipment.equip        equipment.unequip
medical.bandage
survival.rest          survival.sleep
```

The mod rejects anything outside it before dispatch, so a new action cannot be
introduced by a crafted payload — only by changing the schema, the dispatch
table and the capability probe together.

Two subsets of that enum carry rules, both in `protocol/enums.py`:

- `READ_ONLY_ACTIONS` = `world.inspect`, `container.inspect`,
  `inventory.search`, `action.wait`. Permitted in `OBSERVE`; no arming.
  `container.open_nearby` is deliberately not in it — its name reads like a
  query and its body walks the character across a room.
- `ALWAYS_ALLOWED_ACTIONS` = `safety.stop`, `session.disarm`, `plan.cancel`.
  These bypass the arming check entirely.

`lease_ms` is a time-to-live. `CommandQueue.check_lease` — in
`pz_agent_core/ipc/queue.py`, which holds no other queue class — is called once
by `submit` and again by the executor immediately before the command runs. The
second check is the one that matters — a command can sit behind a long timed
action while the world moves on, and executing it late is worse than not
executing it.

## Acknowledgements

`schemas/action_result.schema.json`, `additionalProperties: false`,
`schema_version` const `"1.0"`.

```
received → accepted → started → progress* → succeeded
                    ↘ rejected            ↘ failed / cancelled / lost
```

The `status` enum has all nine of those. Terminal: `succeeded`, `failed`,
`cancelled`, `rejected`, `lost`. Optional fields: `started_at_ms`,
`finished_at_ms` (integer or null), `attempt`, `progress` (0..1 or null),
`message`, `evidence` (an open object), `diagnostics` (array of strings).

**`succeeded` is a claim about the world.** It means a postcondition was
observed. "The command was queued" is `accepted`.

This is enforced structurally rather than by convention:

```python
ActionResult.succeeded(..., evidence={"hunger_before": 0.72, "hunger_after": 0.31})
```

`succeeded()` raises without evidence, and constructing an `ActionResult` with
`status="succeeded"` and any reason other than `POSTCONDITION_MET` raises too.
In the action engine, if the adapter's `verify` never returns evidence the
result is `POSTCONDITION_FAILED` **even when the mod acked `succeeded`**.

Note that the schema itself does not encode that rule: `reason_code` is only
`type: string, minLength: 1`, and `evidence` is only `type: object`. The
invariant lives in `pz_agent_core.protocol.messages`, and the schema is the
looser of the two.

### Reason codes

Stable and append-only. Renaming one is a protocol major bump. Thirty-two
members of `ReasonCode`:

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

`RETRYABLE_CODES` is exactly five: `PATH_STUCK`, `NO_PROGRESS`,
`ACTION_TIMEOUT`, `QUEUE_REJECTED`, `PLAYER_BUSY_MANUAL_ACTION`. Retrying an
`INVALID_REF` or a `POLICY_DENIED` just burns the budget.

`PLAN_FATAL_CODES` is exactly seven — `PANIC_STOP`, `USER_TAKEOVER`,
`GAME_DISCONNECTED`, `SAVE_CHANGED`, `STALE_SESSION`, `SESSION_TERMINATED`,
`NOT_ARMED` — and they stop the whole plan, not merely the current step.

## Observations

`schemas/observation.schema.json`, `schema_version` const `"1.0"`. The largest
of the eight documents and the only one that is not uniformly closed.

Required: `schema_version`, `session_id`, `seq`, `timestamp_ms`, `game`,
`player`, `safety`, `action`. Optional: `full` (boolean, default true),
`inventory`, `nearby`, `capability_revision`, `active_goal_id`.

| Object | Required keys | Additional properties |
| --- | --- | --- |
| top level | as above | forbidden |
| `game` | `build`, `save_id`, `paused`, `speed` | forbidden |
| `player` | `present`, `alive`, `position`, `stats`, `moodles` | forbidden |
| `player.position` | `x`, `y`, `z` | forbidden |
| `player.stats` | — | **allowed**, values number/integer/boolean/null |
| `player.moodles` | — | **allowed**, values non-negative integers |
| `player.wounds[]` | `ref`, `kind`, `severity` | **allowed** |
| `player.hands` | — | forbidden (`primary`, `secondary`) |
| `inventory` | — | forbidden (`containers`, `items`) |
| `nearby.objects[]` | `ref`, `kind`, `distance` | **allowed** |
| `nearby.zombies[]` | `ref`, `distance` | **allowed** |
| `action` | `ownership`, `busy` | forbidden |
| `safety` | `armed`, `mode`, `danger_level`, `manual_takeover` | forbidden |
| `$defs/container` | `ref`, `kind`, `name`, `parent_ref` | forbidden |
| `$defs/item` | `ref`, `container_ref`, `full_type`, `display_name`, `category`, `weight` | **allowed** |

`game.multiplayer` is an optional boolean with its own description in the
schema: omitted means the mod could not read it, which is **not** the same as
`false`, and the sidecar refuses on an omission exactly as it refuses on `true`.

Enums the schema pins: `action.ownership` ∈ `none`/`mod`/`manual`/`ambiguous`;
`safety.mode` ∈ the six `SessionMode` members; `safety.danger_level` ∈
`none`/`low`/`medium`/`high`/`critical`; `$defs/container.kind` ∈ the six
`ContainerKind` members. `$defs/container.ref` must match `^container:` and
`$defs/item.ref` must match `^item:`.

### Observation tiers

| Tier | Content | Who produces it | When |
| --- | --- | --- | --- |
| 0 | Heartbeat: session, seq, versions, player present, armed, mode, active action, danger | The mod | Every tick |
| 2 | Full snapshot | The mod | Every observation interval, on connect, after a gap, after recovery |
| 1 | Compact diff: changed scalars and changed ref lists | **The sidecar**, from two consecutive snapshots | On demand, for the planner and the trace |
| 3 | Requested detail: one container's contents, a wound, a square, a candidate path | The mod | On demand |

**Tier 1 is not a wire format.** The mod writes full snapshots; the sidecar
derives the diff by comparing two of them (`observation/diff.py`). There is no
diff schema in `schemas/`, and a diff is never parsed off the exchange
directory. That is a deliberate trade: full snapshots are idempotent, so a
reader that misses one loses a tick rather than its grip on the world.

### Saying where the walk stopped

A snapshot is built by walking things the player controls the size of, so every
walk in the mod is capped. A capped walk that says nothing is worse than no walk
at all: an inventory list that stops at 512 items reads exactly like an
inventory that holds 512 items.

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
silence on a present section means that section is complete. The mod refuses any
game stat whose name starts with `observe.`.

## Events

`schemas/event.schema.json`, `schema_version` const `"1.0"`, closed. Required:
`schema_version`, `session_id`, `seq`, `timestamp_ms`, `type`, `data`. `data` is
an open object.

`type` is a closed enum of fourteen: `heartbeat`, `observation_diff`,
`action_ack`, `action_progress`, `action_terminal`, `danger_changed`,
`need_changed`, `manual_takeover`, `panic_stop`, `session_changed`,
`plan_changed`, `voice_input`, `voice_output`, `diagnostic`.

Two of those names — `voice_input` and `voice_output` — are the only place the
word "voice" appears in `schemas/`. Nothing in this tree emits or consumes them;
they are a slot the schema reserves. Do not read them as evidence that voice
events cross this journal.

## Plans

`schemas/plan.schema.json`, `schema_version` const `"1.0"`, closed. Required:
`schema_version`, `goal_id` (uuid), `summary` (1–500 chars), `risk_class`
(`P0`…`P4`), `steps`. Optional: `confidence` (0..1).

`steps` is an array of **1 to 5** closed objects, each requiring `step_id`
(matching `^[A-Za-z0-9_-]+$`), `action` (a non-empty string — note the plan
schema does *not* constrain it to the command enum), `args` (open object) and
`success` (an object requiring a `type` string, otherwise open). Optional per
step: `on_failure` ∈ `stop`/`ask_user`/`replan_once`/`skip`, and
`requires_confirmation`.

A plan has **no field for Lua, Python, shell, keystrokes or a file path**. A
plan containing one fails validation because there is nowhere to put it.

## Local Core RPC

The MCP executable is a separate process — an MCP client launches it — so it
reaches the sidecar's ports over a local link. Windows named pipe or Unix
socket; never a network address. `CORE_RPC.md`, in the repository's `docs/`
directory but not in the release archive, describes the transport; the two
schemas describe the messages.

Request (`format` const `pz-agent-core-rpc/1`, `protocol` const `1.0`):

```json
{"format": "pz-agent-core-rpc/1", "protocol": "1.0", "id": "7", "method": "session.status", "params": {}}
```

`id` is 1–64 characters, chosen by the client and echoed back; an answer
carrying a different one is refused rather than matched. `method` is 1–64
characters matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` and must be one the
router publishes. The published set is sixteen names, declared in
`pz_agent_mcp/remote/methods.py` as `Method`, with `ALL_METHODS` derived from
that class rather than restated:

```
session.status  session.arm  session.disarm  session.stop
observation.latest
capabilities.report
action.submit   action.status
plan.execute    plan.current
goal.submit     goal.status     goal.cancel
memory.query
diagnostics.doctor  diagnostics.tail
```

The three `goal.*` verbs are the typed goal channel. There is no `goal.list`
and no `goal.activate`: activation is the channel's own decision, and a listing
would be a second unbounded read of a queue whose point is that it stays small.

Response: `format`, `protocol`, `id` and `ok` are required, and the schema's
`allOf` makes `result` required when `ok` is true and `error` required when it
is false. `ok` is never inferred — a reader that took an absent `error` for
success would turn a dropped field into a success.

`error.code` is a closed enum of seven, with the schema's own division of
labour: `MALFORMED`, `TOO_LARGE` and `PROTOCOL_MISMATCH` mean the message never
reached a method; `UNKNOWN_METHOD` means the router has no such name;
`CORE_REFUSED` carries the core's own refusal; `TIMEOUT` and `UNAVAILABLE` are
the client's own and are never sent by a server. `error.message` names the field
and the reason, never the value — it reaches a traceback and a bug report before
any redactor sees it.

Requests are capped at 64 KiB by `rpc/wire.py`.

---

## Sessions

The sidecar writes `session.json`. The mod accepts it only when **all** of these
hold:

1. `session_id` is a well-formed UUID.
2. The protocol **major** version matches. `protocol_compatible()` is
   major-only, so `1.7` talks to `1.1`; `2.0` is refused.
3. `created_at_ms` is recent — a session file left over from last week is not a
   request to start now.
4. The `nonce` differs from the previous session's. A repeated nonce means a
   stale file was replayed.
5. The sidecar heartbeat is alive.

The mod replies in `heartbeat.game.json` with the same session id and a nonce of
its own. Sessions start in `OBSERVE`; arming is a separate, explicit command.

## Sequences

Four independent monotonic counters: observation, command, ack, event.

A **gap** is never interpolated. When the sidecar sees one it requests a full
snapshot; when the mod sees one it does not attempt to guess the missing
command. Guessing here means executing something the user did not ask for.

Duplicates are recognised by `command_id` and `idempotency_key`. A command whose
key has already reached a terminal result gets that **original result replayed**.
This is what makes at-least-once delivery safe over a file journal.

## Writing without atomic rename

Atomic rename is not guaranteed from inside Kahlua, so full snapshots use two
alternating slots and a pointer:

1. Write the whole document to slot A (or B — whichever is not current).
2. Flush and close.
3. Overwrite the pointer file with the slot name.

The pointer is written last and is small enough that a torn write is detectable.
A reader that finds an invalid document follows the other slot. The worst case
is one stale snapshot, never a half-parsed one.

Journals (`.jsonl`) use a different discipline:

- One record per line, newline-terminated.
- A reader tracks a **byte offset**. A trailing line with no newline yet is
  ignored and re-read next tick — the writer is still mid-write.
- A complete but unparseable line is reported as a corrupt record and skipped,
  so one bad line cannot stall the stream forever.
- Files are size-capped and rotate; rotation is signalled to the reader rather
  than silently losing records.

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
six colon-separated segments — the literal word `world` and then five numbers,
`world:<x>:<y>:<z>:<object-index>:<container-index>` — so an item reference
carrying one splits into ten segments where the line above reads as five.
Parsing an item reference with a naive `split(":")` therefore does not fail
loudly — it produces a *different valid reference*, pointing at some other
object. Both the Python and the Lua
implementations parse from the ends and treat everything between as the opaque
container tail.

**Generation.** Anything that can invalidate object identity — a save/load
transition, a new session — bumps the generation. A reference minted before the
transition then fails validation instead of resolving to whatever now occupies
that runtime id. References are also session-scoped: one from a previous session
is `INVALID_REF`, not a retryable miss.

## Capabilities

`capabilities.json` records **probe results**, not intentions. It has no JSON
Schema in `schemas/`; its shape is defined by
`pz_agent_core/capabilities/model.py`.

```json
{
  "build": "42.20",
  "protocol_version": "1.1",
  "revision": 3,
  "capabilities": {
    "move_to_square":      {"state": "verified"},
    "inventory_transfer":  {"state": "verified"},
    "eat_percentage":      {"state": "verified"},
    "drink_world_source":  {"state": "experimental"},
    "autonomous_attack":   {"state": "unsupported", "reason": "NO_VERIFIED_API"}
  }
}
```

| State | Meaning | Usable? |
| --- | --- | --- |
| `verified` | A probe ran against the live game and confirmed it | yes |
| `available_unverified` | The symbols exist in the local files; nothing has exercised them | yes |
| `experimental` | Symbols present, driving them is not proven safe | no |
| `unsupported` | No verified API. The reason is recorded | no |
| `disabled_by_policy` | Available, and listed in `safety.disabled_capabilities` | no |

"Usable" is `CapabilityState.usable`, and it is what decides whether an MCP write
tool is published. See [`COMPATIBILITY.md`](COMPATIBILITY.md) for the probes
themselves.

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

Nothing in this table re-arms the agent automatically.

## Privacy

The protocol does not carry the Windows username, absolute paths, Steam tokens,
chat text, the process list, or the contents of arbitrary files. `save_id` is a
truncated hash rather than a path. Diagnostic bundles redact paths and secrets
before they are written, not on the way out.

See [`PRIVACY.md`](../PRIVACY.md) for what an external LLM provider would see if
you enable one, and [`SAFETY.md`](SAFETY.md) for where each rule is enforced.
