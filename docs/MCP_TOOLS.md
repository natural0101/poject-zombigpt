# MCP tools and resources

The MCP server is a **thin adapter**. It translates tool calls into core
commands and serialises domain errors back out. It does not re-implement policy,
does not decide what is safe, and does not hold state that core does not already
hold. A boundary that duplicates the engine's rules is a boundary that will
eventually disagree with it.

> **Status:** this document specifies the boundary. See
> [`PROGRESS.md`](PROGRESS.md) for which tools are implemented and wired.

Transport: stdio.

---

## Semantics that apply to every tool

**Typed structured content.** Every tool returns a structured result, not prose.
Errors carry a stable `reason_code` from the protocol's closed set — the same
codes the action engine and the mod use, so a failure means the same thing
wherever you read it.

**Write tools require an armed session.** Except the ones that cannot: `stop` is
always available, in every mode, armed or not.

**Read tools work in `OBSERVE`.** Observation never requires arming.

**Long-running tools return an action id.** Movement, eating and reading take
game-seconds. The tool returns immediately with an id; you poll it with the same
idempotency key, which replays the call and refreshes it to the action's current
state. It does not block the transport and it does not report success early.

**Idempotency.** Calling a write tool twice with the same idempotency key does
not perform the action twice — the original terminal result is replayed.

**Capability gating.** A tool whose capability is `unsupported` or
`experimental` is not published as ready. It appears in the capability report
with its reason, rather than being offered and then failing.

**No internal primitives.** Nothing is exposed that would let a caller route
around the policy layer. If a tool would let you do by composition what policy
forbids directly, it is not published.

---

## Tools

### Session

| Tool | Risk | Description |
| --- | --- | --- |
| `pz_session_status` | P0 | Mode, armed state, session id, heartbeat health, build, capability revision |
| `pz_session_arm` | — | Move to `ASSISTED` or `AUTONOMOUS`. Requires an existing backup for autonomous |
| `pz_session_disarm` | — | Return to `OBSERVE`. Always permitted |

### Observation

| Tool | Risk | Description |
| --- | --- | --- |
| `pz_observe_snapshot` | P0 | Full current state, compacted for a model |
| `pz_observe_inventory` | P0 | Container tree with stable refs, recursing into nested carried containers |
| `pz_observe_nearby` | P0 | World objects and zombies within a bounded radius, with semantics |

Every observation returned here has passed through `observation/compact.py`: no
absolute paths, no OS username, no save paths, no raw chat or book text. Item
display names are carried as **untrusted data** and marked as such.

### Actions

| Tool | Risk | Verified by |
| --- | --- | --- |
| `pz_action_move_to` | P2/P3 | Character position within the target radius, correct floor |
| `pz_action_transfer` | P1 | Item ref resolves inside the destination container |
| `pz_action_eat` | P2 | Hunger decreased, or item uses decremented |
| `pz_action_drink` | P2 | Thirst decreased, or container volume decreased |
| `pz_action_read` | P2 | Reading started and progress observed |
| `pz_action_wait` | P0 | Observed elapsed game time |
| `pz_action_cancel` | — | Mod-owned entry no longer in the action queue |

The "verified by" column is not documentation of intent — it is the
postcondition the action engine actually checks. If it does not hold, the result
is `POSTCONDITION_FAILED`, even when the mod acked success.

Note what is absent: eating takes an item ref *and optionally a fraction*, but
there is no tool to choose *which* item. That decision belongs to
`policy/food.py`, which is deterministic and testable. A model that picks the
sandwich is a model that will eventually pick the rotten one.

Also absent: `allow_windows`. The movement adapter refuses it with
`POLICY_DENIED`, so publishing it would advertise something policy forbids.

### Plans

| Tool | Risk | Description |
| --- | --- | --- |
| `pz_plan_execute` | varies | Submit a typed plan. Validated against the plan schema before anything runs |
| `pz_plan_status` | P0 | Current step, results so far, why the plan stopped |

A plan is a list of typed steps. It has no field for Lua, Python, shell,
keystrokes or file paths — a plan containing one fails validation, because
there is nowhere to put it.

Neither plan tool ever puts `succeeded` in the envelope `status`: that word is
reserved for a result carrying the observed postcondition under `data.evidence`,
and a plan record has none to carry — its steps' evidence was observed by the
action engine and stops at the port. A plan that finished answers `"ok"`, and
`data.status` with `data.terminal` say what it finished as.

### Safety

| Tool | Risk | Description |
| --- | --- | --- |
| `pz_safety_stop` | — | **Always available.** Bypasses the queue, clears mod-owned entries only, disarms |

`pz_safety_stop` works when the session is unarmed, when the planner is absent,
and when the queue is backed up. That is the whole point of it.

### Memory and diagnostics

| Tool | Risk | Description |
| --- | --- | --- |
| `pz_memory_query` | P0 | Known containers, home point, safe zones, failed paths, user reservations |
| `pz_debug_doctor` | P0 | Full environment report with stable check codes |
| `pz_debug_tail` | P0 | Recent structured log records, redacted |

---

## Resources

| URI | Content |
| --- | --- |
| `pz://session/current` | Session id, mode, armed state, protocol version |
| `pz://observation/latest` | Most recent compacted snapshot |
| `pz://inventory/current` | Container tree with stable refs |
| `pz://capabilities` | Probe results and their evidence |
| `pz://plan/current` | Active plan and step results |
| `pz://safety/status` | Danger level, takeover state, heartbeat health |
| `pz://diagnostics/recent` | Recent diagnostics, redacted |

Resources are read-only views over state core already holds. Each read carries
the observation `seq` it was built from, which a client uses as an ETag.

**Subscriptions are not delivered yet.** The design calls for them — a client
watching `pz://safety/status` rather than polling it, because a safety change is
exactly the thing you do not want to learn about on the next poll interval — but
the server registers no `subscribe_resource` handler and nothing in core
publishes resource-change events. Every resource descriptor therefore reports
`subscribable: false`. A client that could subscribe and was never notified
would read the silence as "nothing has changed", which is the worst possible
failure for the safety view. Until the event source exists, poll.

---

## Result shape

```json
{
  "ok": true,
  "tool": "pz_action_eat",
  "request_id": "7f1c…",
  "status": "accepted",
  "message": "consume.eat is accepted",
  "data": {},
  "warnings": [],
  "replayed": false,
  "action_id": "b2a9…"
}
```

`status` is an `ActionStatus` value for the long-running tools and `"ok"` for
the ones that answer immediately. `action_id` is present exactly when the call
put work in flight. `replayed` is true when an idempotency key was reused and
the answer is the original call's, not a second action.

`status: "succeeded"` cannot be constructed without the observed postcondition
under `data.evidence` — the same rule as `ActionResult.succeeded()`, enforced
again where a client reads it.

## Error shape

```json
{
  "ok": false,
  "tool": "pz_action_eat",
  "request_id": "7f1c…",
  "reason_code": "NOT_ARMED",
  "message": "session is in OBSERVE; call pz_session_arm first",
  "retryable": false,
  "diagnostics": []
}
```

`reason_code` is from the protocol's closed set and is stable across releases —
renaming one is a protocol major bump. `retryable` reflects
`RETRYABLE_CODES`, so a client does not have to maintain its own table of which
failures are worth another attempt.

Every mutating tool takes `idempotency_key` (required). Every one that submits a
single command also takes `timeout_ms` (optional, that command's lease);
`pz_plan_execute` does not, because a plan is bounded by
`limits.max_real_seconds` rather than by a lease, and publishing an argument no
handler reads would be the same lie as publishing a bound nothing enforces.

No mutating tool takes a free-text field: `pz_plan_execute`'s `goal` is the only
string a caller may write in their own words, and it comes back quarantined.

A replay carries `replayed: true` and the status of the call it is replaying —
refreshed from the action if one is still in flight, and otherwise the status the
first call answered with. It is never downgraded to a bare `"ok"`.

## What a caller cannot do

- Execute code. There is no field for it anywhere in the surface.
- Name a file. Every IPC filename is a hardcoded constant on both sides.
- Act while disarmed. Except `stop` and `disarm`, which are always available.
- Use an unverified capability without opting into `EXPERIMENTAL_INPUT`.
- Bypass policy by composing primitives. Primitives that would allow it are not
  published.
