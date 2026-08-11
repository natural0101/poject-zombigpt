# MCP tools and resources

**This document is generated from `pz-agent-mcp --describe` and is checked
against it.** Every name, kind, risk class, arming requirement, argument and
bound below was read out of that command's JSON on this build. If you are
writing a client, run it yourself — it needs no game, no sidecar and no MCP SDK:

```powershell
.venv\Scripts\pz-agent-mcp --describe
```

The MCP server is a **thin adapter**. It translates tool calls into core
commands and serialises domain errors back out. It does not re-implement policy,
does not decide what is safe, and does not hold state that core does not already
hold. A boundary that duplicates the engine's rules is a boundary that will
eventually disagree with it.

Transport: stdio. The server registers itself as `pz-agent`.

| Field of `--describe` | Value on this build |
| --- | --- |
| `server` | `pz-agent` |
| `product_version` | `0.1.0` |
| `protocol_version` | `1.1` |
| `capability_gated` | `true` |
| tools | 41 |
| resources | 7 |

`--describe` reports the **whole** catalogue. A running server publishes a
subset of it: a tool whose capability is not usable on the install is withheld.
See *Capability gating* below.

---

## Semantics that apply to every tool

**Every input schema is closed.** `additionalProperties` is `false` on all
41 tools, so an argument this document does not list is rejected rather
than ignored.

**The advertised bound is the enforced bound.** `catalog.py` imports its numbers
from the adapters that will receive the arguments and validates each tool's own
example against its own schema at import time, so a value this surface accepts
is one the adapter accepts.

**Typed structured content.** Every tool returns a structured result, not prose.
Errors carry a stable `reason_code` from the protocol's closed set — the same
codes the action engine and the mod use.

**`kind` decides arming, not the name.** `requires_armed` is `true` for exactly
the `write` tools and for nothing else:

| Kind | Count | Arming | What it is |
| --- | --- | --- | --- |
| `read` | 11 | not required | Answered from state the sidecar already holds |
| `query` | 3 | not required | Submits one of the protocol's `READ_ONLY_ACTIONS`; comes back with an action id, but the character neither moves nor touches anything |
| `write` | 21 | **required** | Changes the world; refused with `NOT_ARMED` on a disarmed session |
| `control` | 6 | not required | Arm, disarm, cancel, stop and the goal verbs — how a disarmed or panicking session is driven |

`pz_action_open_container` is a `write` tool, whatever its name suggests: it
walks the character across a room. `ToolSpec.__post_init__` refuses to construct
a `query` tool whose action is not in `READ_ONLY_ACTIONS`, so that classification
cannot be talked around.

One thing worth knowing before you read the table: `action.wait` **is** in the
protocol's `READ_ONLY_ACTIONS`, and `pz_action_wait` is nevertheless published
as `write` with `requires_armed: true`. That is what the build does; this
document does not soften it. Waiting on a disarmed session is refused.

**`risk` is the base tier, not a worst case.** It is the tier the adapter
declares. Several adapters assess higher per call — `movement.move_to` becomes
`P3` when the destination changes floor or leaves the safe radius, and both
transfer forms, `inventory.transfer` and `inventory.transfer_batch`, become
`P3` when a source is a world container — and none of that is visible from the
tool name, so none of it is published.

**Long-running tools return an action id.** `long_running` is `true` for
23 tools. The call returns immediately with an `action_id`; you poll it with
`pz_action_status`, wait on it with `pz_action_await`, or call again with the
same `idempotency_key`, which replays the call and refreshes it to the action's
current state. It does not block the transport and it does not report success
early.

**Idempotency.** Every tool that submits a command takes a required
`idempotency_key`; 27 tools do. Twenty-five of them accept 1–120 characters with
no further shape; the two goal verbs, `pz_goal_submit` and `pz_goal_cancel`,
take 1–64 matching `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$`, because the goal
channel carries the key into its own record and a key is not a place to smuggle
text. Calling twice with the same key does not perform the action twice — the
original result is replayed with `replayed: true`.

**`timeout_ms`** is that one command's lease: integer, 100–300000 ms, default
15000. Twenty-four tools take it — every one that submits a single command.
`pz_plan_execute` does not, because a plan is bounded by
`limits.max_real_seconds` instead, and publishing an argument no handler reads
would be a lie shaped like an option. One tool reuses the name for a different
budget: on `pz_action_await`, `timeout_ms` is that call's own wait budget
(100–60000 ms, default 5000), not a lease — its section says so beside the
number.

**No free text.** `pz_plan_execute`'s `goal` (1–200 characters) is the only
field in the whole surface a caller may write in their own words, and it comes
back quarantined. Every other string argument is a ref matching a fixed pattern,
an enum member, a lower-case token matching
`^[a-z][a-z0-9_.\-]{0,63}$`, or — in exactly one place, `pz_goal_submit`'s
`categories` — a comma-joined list of closed upper-case tokens whose pattern
names every admissible spelling.

---

## The published surface

| Tool | Kind | Risk | Armed | Long-running | Capability gate |
| --- | --- | --- | --- | --- | --- |
| `pz_session_status` | read | P0 | no | no | — |
| `pz_session_arm` | control | P0 | no | no | — |
| `pz_session_disarm` | control | P0 | no | no | — |
| `pz_observe_snapshot` | read | P0 | no | no | — |
| `pz_observe_inventory` | read | P0 | no | no | — |
| `pz_observe_nearby` | read | P0 | no | no | — |
| `pz_action_inspect_world` | query | P0 | no | yes | — |
| `pz_action_inspect_container` | query | P0 | no | yes | — |
| `pz_action_search_inventory` | query | P0 | no | yes | — |
| `pz_action_move_to` | write | P3 | yes | yes | `move_to_square` |
| `pz_action_move_near` | write | P3 | yes | yes | `move_to_square` |
| `pz_action_open_container` | write | P3 | yes | yes | `move_to_square` |
| `pz_action_open_door` | write | P3 | yes | yes | `door_toggle` |
| `pz_action_close_door` | write | P3 | yes | yes | `door_toggle` |
| `pz_action_unlock_door` | write | P3 | yes | yes | `door_toggle` |
| `pz_action_transfer` | write | P1 | yes | yes | `inventory_transfer` |
| `pz_action_transfer_batch` | write | P1 | yes | yes | `inventory_transfer` |
| `pz_action_ensure_main` | write | P1 | yes | yes | `inventory_transfer` |
| `pz_action_eat` | write | P2 | yes | yes | `eat_percentage` |
| `pz_action_drink` | write | P2 | yes | yes | `drink_carried` |
| `pz_action_drink_source` | write | P2 | yes | yes | `drink_world_source` |
| `pz_action_read` | write | P2 | yes | yes | `read_literature` |
| `pz_action_equip` | write | P2 | yes | yes | `equipment_equip` |
| `pz_action_unequip` | write | P2 | yes | yes | `equipment_unequip` |
| `pz_action_bandage` | write | P2 | yes | yes | `medical_bandage` |
| `pz_action_rest` | write | P2 | yes | yes | `survival_rest` |
| `pz_action_sleep` | write | P4 | yes | yes | `survival_sleep` |
| `pz_action_wait` | write | P0 | yes | yes | — |
| `pz_action_cancel` | control | P1 | no | yes | — |
| `pz_action_cancel_all` | control | P1 | no | no | — |
| `pz_action_status` | read | P0 | no | no | — |
| `pz_action_await` | read | P0 | no | no | — |
| `pz_plan_execute` | write | P2 | yes | no | — |
| `pz_plan_status` | read | P0 | no | no | — |
| `pz_goal_submit` | write | P2 | yes | no | — |
| `pz_goal_status` | read | P0 | no | no | — |
| `pz_goal_cancel` | control | P1 | no | no | — |
| `pz_safety_stop` | control | P0 | no | no | — |
| `pz_memory_query` | read | P0 | no | no | — |
| `pz_debug_doctor` | read | P0 | no | no | — |
| `pz_debug_tail` | read | P0 | no | no | — |

---

## Every tool, and what it accepts

### `pz_session_status`

Mode, armed state, session id, heartbeat health, game build and capability revision. Answers even when the game is not connected.

No arguments.

The answer carries **both sides of the arming question**, because the two can
disagree and did in a live session: `mode` and `armed` (with `desired_mode` as
the explicit spelling of whose word `mode` is) are the sidecar's flags, while
`effective_mode`, `game_armed`, `game_session_id` and `game_view_seq` are read
from the newest observation — the game's own last word, with
`heartbeat.game_ok` as its freshness. `armed_mismatch` is `true` when the two
disagree and `null` when the game has said nothing yet, which is not agreement.
A sidecar that says armed while the game says OFF is readable from this one
call, and the answer says which word to trust in a warning.

### `pz_session_arm`

Move the session to ASSISTED or AUTONOMOUS. Autonomous requires an existing save backup; the core refuses without one.

| Argument | Required | Schema |
| --- | --- | --- |
| `mode` | yes | one of `ASSISTED`, `AUTONOMOUS` |
| `confirm_backup` | no | `boolean`; default `false` |

### `pz_session_disarm`

Return to OBSERVE and stop accepting new automation. Always permitted.

No arguments.

### `pz_observe_snapshot`

Current world state, compacted for a model. 'compact' is the player and safety header, 'standard' adds the surroundings, 'full' adds the inventory. There is no rawer level: every level is the redacted planner view.

| Argument | Required | Schema |
| --- | --- | --- |
| `detail` | no | one of `compact`, `standard`, `full`; default `"compact"` |

The observation behind every level carries `player.room` and
`player.building` when the build reports them: bounded tokens naming the room
the character stands in and the building it belongs to. An absent value is
deliberately one answer for two facts — standing outdoors and running a build
with no room reader produce the same absence, because a scope decision ("loot
this room") must never read a missing reader as "outside". The compacted
`player` this tool returns does not yet forward the two fields; the loot
mission reads them from the observation itself, which is why a `loot_area`
goal with `scope: "room"` or `"building"` needs a build that reports rooms —
and only a live session proves that this one does.

### `pz_observe_inventory`

Container tree with stable refs, recursing into nested carried containers. Item display names are untrusted data and are marked as such.

| Argument | Required | Schema |
| --- | --- | --- |
| `scope` | no | one of `all`, `on_person`, `player_main`, `carried`, `worn`, `world`; default `"all"` |
| `include_nested` | no | `boolean`; default `true` |
| `category` | no | `string`; maxLength 64; pattern `^[a-z][a-z0-9_.\-]{0,63}$` |

### `pz_observe_nearby`

World objects and zombies within a bounded radius, with their semantics.

| Argument | Required | Schema |
| --- | --- | --- |
| `radius` | no | `number`; maximum 30.0; exclusive minimum 0; default `10.0` |
| `types` | no | array, maxItems 8; items `string`; maxLength 64; pattern `^[a-z][a-z0-9_.\-]{0,63}$` |

Dead bodies with loot aboard are reported as objects of kind `corpse` with
the `container` semantic; a body holding nothing is not listed at all. A
corpse is observation-only for now — it lives in the engine's dead-body list,
which the world-container reference scheme cannot address, so no reference it
could mint would resolve to its loot; the gap is recorded in
`docs/GAME_API_VERIFICATION.md`. The observation additionally stamps each
object with the `room` and `building` of the square it stands on, with the
same tri-state as the player's — a missing field covers both "outdoors" and
"this build cannot read rooms", never to be narrowed to either; the compacted
objects this tool returns do not yet forward the two fields, and the loot
mission reads them from the observation itself.

### `pz_action_inspect_world`

Describe the block of squares around a centre, with what the mod makes of each one. Omit 'ref' to look around the character. Nothing moves: an inspect that walked round a corner to see better would be a mutating command wearing a read-only command's permissions.

| Argument | Required | Schema |
| --- | --- | --- |
| `ref` | no | `string`; maxLength 220; pattern `^square:[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `integer`; minimum 0; maximum 2; default `1` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_inspect_container`

List what one container holds, with the real total beside the bounded listing. Reads the engine's own item list; no UI is driven and nothing is opened, so the character does not move.

| Argument | Required | Schema |
| --- | --- | --- |
| `container_ref` | yes | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
| `limit` | no | `integer`; minimum 1; maximum 64; default `64` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_search_inventory`

List what the character is carrying that matches a filter. Every reference returned resolves inside the character's own containers, so a result is something the next step can act on without walking anywhere. It reports what matches; it never picks one.

| Argument | Required | Schema |
| --- | --- | --- |
| `full_type` | no | `string`; maxLength 128; pattern `^[A-Za-z0-9._\-]{1,128}$` |
| `type_prefix` | no | `string`; maxLength 128; pattern `^[A-Za-z0-9._\-]{1,128}$` |
| `edible` | no | `boolean` |
| `drinkable` | no | `boolean` |
| `readable` | no | `boolean` |
| `exclude_equipped` | no | `boolean`; default `false` |
| `limit` | no | `integer`; minimum 1; maximum 32; default `32` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_move_to`

Walk to a square. Verified by the character's observed position being within the radius on the correct floor.

| Argument | Required | Schema |
| --- | --- | --- |
| `target` | yes | object with integer `x`, `y`, `z`, all three required, no other keys |
| `radius` | no | `number`; maximum 3.0; exclusive minimum 0; default `0.75` |
| `max_distance` | no | `integer`; minimum 1; maximum 30; default `20` |
| `allow_doors` | no | `boolean`; default `true` |
| `allow_stairs` | no | `boolean`; default `true` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

`allow_doors` now travels to the mod with the command. It used to be a
sidecar-only flag — accepted here, and then kept on this side with the other
policy flags, so the mod defaulted the door policy silently at the far end.
The doors epic moved it to the wire, because it is the one flag whose decision
cannot finish on this side: whether a closed door stands on the route is
something only the mod discovers, mid-walk. `max_distance` and `allow_stairs`
still stay on this side, deliberately: they are decisions the adapter has
already made against the observation.

### `pz_action_move_near`

Walk to within interaction range of something in the world. Verified against the object's *re-observed* position, not the one it had when the call was made: an object that is no longer in view cannot be proven to be within arm's reach.

| Argument | Required | Schema |
| --- | --- | --- |
| `object_ref` | yes | `string`; maxLength 220; pattern `^(?:container\|square\|item):[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.5` |
| `max_distance` | no | `integer`; minimum 1; maximum 30; default `20` |
| `allow_doors` | no | `boolean`; default `true` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

`allow_doors` ships with the command here too — both move commands share the
mod's walk loop, so both meet the same closed doors on the way.

### `pz_action_open_container`

Get within reach of a world container, so its contents can be taken. Its name reads like a query and it is not one: this walks the character across a room, so it needs an armed session like any other move. A door in the way is not opened — that is a different object and a different action.

| Argument | Required | Schema |
| --- | --- | --- |
| `container_ref` | yes | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.6` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_open_door`

Open a named door. Verified by the following observation describing it open; a door already open comes back as an unchanged success, not an error. A door observed locked is refused with DOOR_LOCKED — it needs its key (`pz_action_unlock_door`) before it will open — and one observed barricaded with DOOR_BARRICADED, which no toggle fixes.

| Argument | Required | Schema |
| --- | --- | --- |
| `door_ref` | yes | `string`; maxLength 220; pattern `^object:[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.6` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

`door_ref` comes from `pz_observe_nearby`, which reports each door as an
`object:` reference — `object:<session>:<x>:<y>:<z>:<object_index>`, the
square it stands on and its index in that square's object list — together with
its tri-state `open`, `locked` and `barricaded` fields. An absent field means
the build exposes no reader for that fact, never `false`; the pre-flight
refusals fire only on an observed `true`, and an unreadable state is passed
through for the mod to judge against the engine object. The three door tools
share this shape and this rule, and a "merely closed" door is not an error
anywhere in them.

### `pz_action_close_door`

Close a named door. Verified by the following observation describing it closed. A lock never blocks this — a lock holds a door closed — so the one state refusal is DOOR_BARRICADED, and a door already closed comes back as an unchanged success.

| Argument | Required | Schema |
| --- | --- | --- |
| `door_ref` | yes | `string`; maxLength 220; pattern `^object:[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.6` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_unlock_door`

Unlock a named door. A matching key must be observably usable — the mod checks the character's own key ring against the engine's key ids — and a locked door with no such key aboard answers DOOR_LOCKED: a key hunt, not a retry. A barricaded door answers DOOR_BARRICADED, which is a detour. Verified by the following observation reporting the lock off; a door already unlocked is an unchanged success.

| Argument | Required | Schema |
| --- | --- | --- |
| `door_ref` | yes | `string`; maxLength 220; pattern `^object:[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.6` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_transfer`

Move one item into a container. Verified by the item resolving inside the destination and nowhere else.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `destination_container_ref` | yes | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
| `source_container_ref` | no | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_transfer_batch`

Move up to eight named items into one container, each by the game's own transfer, with capacity re-checked before every item and the batch stopped at the first that would not fit. Succeeded only when every requested item is observed in the destination afterwards; a stop partway is a CONTAINER_FULL failure whose evidence carries the honest partial record — what landed, what stopped, and why. Each reference moves as one item, exactly as `pz_action_transfer` moves it.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_refs` | yes | array; minItems 1; maxItems 8; uniqueItems; items `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `destination_container_ref` | yes | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

The items may live in different source containers; the destination is the one
thing they share. `succeeded` means **all of them**: the evidence
(`items_in_destination_container`) reports `destination_ref`, `requested`,
`transferred` — the references observed in the destination — `stopped`, each
entry an item with its `reason_code` and detail, and `destination_count_delta`.
A batch the capacity check ends partway is a **failed** terminal with
`CONTAINER_FULL` carrying that same record: three items in and five stopped is
never reported as anything but three in and five stopped, and what to do with
the remainder — free space, pick another container, shorten the list — is the
planner's decision, made on the record rather than on a rounded-up claim.
Duplicates are refused by the schema (`uniqueItems`), because a reference named
twice has no second transfer to perform.

### `pz_action_ensure_main`

Bring one item into the main inventory. This is the preparation step eating, drinking, reading, equipping and bandaging all require, and it is its own action with its own evidence rather than something those adapters do on the side.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_eat`

Eat a named item. Verified by hunger falling or the item's uses decrementing. Which item is safe to eat is decided by core policy, not here: there is no tool that chooses one.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `fraction` | no | `number`; minimum 0.05; maximum 1.0; default `1.0` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_drink`

Drink from a named carried item. Verified by thirst falling or the container's volume decreasing.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `fraction` | no | `number`; minimum 0.05; maximum 1.0; default `1.0` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_drink_source`

Fill a carried vessel at a sink, well or rain collector and drink from it. Verified by thirst falling; the vessel's own volume proves nothing here, because the fill raises it and the drink lowers it again.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `fraction` | no | `number`; minimum 0.05; maximum 1.0; default `1.0` |
| `source_ref` | yes | `string`; maxLength 220; pattern `^square:[A-Za-z0-9:_.\-]{1,200}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_read`

Read a named book. Verified by the observed page counter advancing.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `pages` | no | `integer`; minimum 1; maximum 200; default `20` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_equip`

Put one item in a hand or on the body. Omit 'hand' for anything the character wears: the item's own body location is what decides between a hand and a slot, and naming a hand for a garment would refuse every garment. Verified by the requested slot holding it.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `hand` | no | one of `both`, `primary`, `secondary` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_unequip`

Take one item off and keep it. Name it exactly one way — by item, by hand or by slot — because the three can disagree and there is no defensible rule for which would win. Verified by no slot holding it *and* it still being on the character: an item that left the hand and the inventory was dropped, not unequipped.

| Argument | Required | Schema |
| --- | --- | --- |
| `item_ref` | no | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `hand` | no | one of `primary`, `secondary` |
| `slot` | no | `string`; maxLength 64; pattern `^[A-Za-z0-9._\-]{1,64}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_bandage`

Dress one bleeding wound with one carried dressing. Verified by the named body part no longer being reported as bleeding — never by the dressing leaving the inventory, which is equally true of one that was dropped. A part that is not bleeding is refused rather than attempted: the observation carries no dressing state to check against. Which part and which dressing are core policy's decision.

| Argument | Required | Schema |
| --- | --- | --- |
| `body_part` | yes | one of `Foot_L`, `Foot_R`, `ForeArm_L`, `ForeArm_R`, `Groin`, `Hand_L`, `Hand_R`, `Head`, `LowerLeg_L`, `LowerLeg_R`, `Neck`, `Torso_Lower`, `Torso_Upper`, `UpperArm_L`, `UpperArm_R`, `UpperLeg_L`, `UpperLeg_R` |
| `item_ref` | yes | `string`; maxLength 220; pattern `^item:[A-Za-z0-9:_.\-]{1,200}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_rest`

Recover endurance up to a target. Verified by the endurance reading rising to it — or, on a build that reports no endurance, by the character being observed sitting, but only if sitting is what was asked for. A standing rest with no readable stat has nothing to show for itself and times out.

| Argument | Required | Schema |
| --- | --- | --- |
| `target_endurance` | no | `number`; minimum 0.05; maximum 1.0; default `0.9` |
| `seat_ref` | no | `string`; maxLength 220; pattern `^square:[A-Za-z0-9:_.\-]{1,200}$` |
| `allow_ground` | no | `boolean`; default `false` |
| `max_wait_ms` | no | `integer`; minimum 1000; maximum 900000; default `300000` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_sleep`

Sleep a night off in a named bed. The most consequential action in this build, and the reason it is P4: once the character is asleep the mod cannot wake them — sleep runs through the bed's context menu, so there is no timed action to interrupt and no queue entry to cancel, and a panic stop cannot reach it. It is refused outright while the guard reports any danger at all, and it is never taken on the agent's own initiative. Its capability is 'experimental' on a clean scan, so on most installs this tool is withheld rather than offered. Verified by fatigue falling *and* the world clock advancing; fatigue alone is a quiet afternoon.

| Argument | Required | Schema |
| --- | --- | --- |
| `bed_ref` | no | `string`; maxLength 220; pattern `^square:[A-Za-z0-9:_.\-]{1,200}$` |
| `hours` | no | `integer`; minimum 1; maximum 16; default `8` |
| `allow_vehicle_seat` | no | `boolean`; default `false` |
| `max_wait_ms` | no | `integer`; minimum 1000; maximum 1800000; default `600000` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_wait`

Hold still until the world clock has advanced. Verified against observed game time, never the sidecar's wall clock.

| Argument | Required | Schema |
| --- | --- | --- |
| `game_seconds` | yes | `number`; maximum 3600.0; exclusive minimum 0 |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_cancel`

Cancel a mod-owned action. Verified by no mod-owned entry remaining in the queue; an action the player queued is never touched.

| Argument | Required | Schema |
| --- | --- | --- |
| `command_id` | no | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_cancel_all`

Clear every mod-owned entry in one call: the mass form of `pz_action_cancel`, with nothing narrower to mis-aim. Ownership is the mod's own tag, so an action the player queued is never touched, and the postcondition is negative — no entry this session owns still in flight — so a second call finds it already true and succeeds clearing nothing. Returns the cancel's action id; `pz_action_await` turns it into the engine's verdict.

| Argument | Required | Schema |
| --- | --- | --- |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

The answer reports `scope: "mod_owned"` and `requested_reason:
"CANCELLED_BY_REQUEST"` — the reason the loop's levers record against each
submission and in-flight command they end. `cancelled_counts` answers
`{"channel_pending": null, "in_flight": null}`: the per-layer counts are
recorded by the loop against the records it terminalises and no port carries
them back, and `null` means uncounted, never zero — the same rule that keeps
the panic stop's `cleared` at what was observed. It differs from
`pz_safety_stop` on exactly one axis: nothing here disarms.

### `pz_action_status`

The current record of one submitted action: its status, its terminal result, and — for an observed success — its evidence. An id this sidecar does not hold is answered as `known: false` with the likely causes, not as an error: the record store is a bounded ring that evicts finished work, and a restarted sidecar holds nothing the previous process minted, so unknown here is a routine fact and never means the action did not run.

| Argument | Required | Schema |
| --- | --- | --- |
| `action_id` | yes | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |

A known id answers the same shape a submit does — `data.status`,
`data.terminal`, `reason_code`, the quarantined `detail` and, on an observed
success, `data.evidence` — plus `known: true`, with the record's status in the
envelope `status`. An unknown id answers `known: false`, `status: null`,
`terminal: null` and `likely_causes: ["evicted", "sidecar_restarted"]` — a
typed answer an agent loop can branch on, deliberately not a refusal.

### `pz_action_await`

Wait, bounded, for a submitted action to reach a terminal state, re-reading its record on a small interval (50 ms; no busy spin, no lock held across the wait — the stop tools stay reachable while it runs). Answers the `pz_action_status` shape plus `waited_ms` and `timed_out`; a budget that ends first reports the record as it stands with `timed_out: true`, and an unknown id answers immediately.

| Argument | Required | Schema |
| --- | --- | --- |
| `action_id` | yes | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |
| `timeout_ms` | no | `integer`; minimum 100; maximum 60000; default `5000` — **this call's wait budget, not the action's lease** |

`timed_out: true` is the *call's* end, not the action's: the record is still
reported as it stands, and calling again keeps waiting. `timed_out: false` with
`known: false` means the id is unknown here and waiting longer cannot help.

### `pz_plan_execute`

Submit a goal for the typed planner. The plan is validated before anything runs. There is no field for raw steps, code or file paths.

| Argument | Required | Schema |
| --- | --- | --- |
| `goal` | yes | `string`; minLength 1; maxLength 200 |
| `mode` | no | one of `ASSISTED`, `AUTONOMOUS`; default `"ASSISTED"` |
| `limits` | no | object; `max_steps` integer 1–8 default 8, `max_real_seconds` integer 1–600 default 120, no other keys |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |

### `pz_plan_status`

Current step, the results so far, and why the plan stopped.

No arguments.

### `pz_goal_submit`

Ask the typed goal channel for one of the things it carries. The kind set is closed and there is no free-text field at all: an invented kind is refused, never approximated. The channel admits the goal to a bounded backlog and answers with its id and state — 'pending' is the honest word for a goal nothing has started yet, and every goal carries a wall-clock, step and time-to-live budget so that it reaches a terminal state whether or not it is served. Which sandwich satisfies a hunger goal is never decided here. A 'loot_area' goal finishes on one provable criterion — every reachable container in scope was inspected or has a recorded skip reason — and its terminal answer reports the looted scope (the pinned room, building or sweep), the containers inspected, the containers skipped each with its reason, and the items taken per category and left per reason. An 'explore_area' goal finishes on the matching criterion — no frontier square remains in scope: every scope square is known to the local map or carries a recorded skip reason — and its report carries the pinned scope, the map growth (cells_discovered), the waypoints visited, and each skipped square with its reason (a locked or barricaded door named by reference, a proven no-route). A 'return_home' goal takes no parameters at all: the target is the save's remembered home point ('pz-agent remember home'), no home set is a typed PRECONDITION_FAILED whose detail is the remedy, and the goal succeeds only on the observed arrival. The care kinds are deterministic missions over the medical and survival adapters: 'treat_wounds' (no parameters — triage and dressing choice are policy, remade per observation) dresses every observed bleeding wound and finishes only when none bleeds, with dressings running out a typed partial failure naming the honest count; 'rest_until' sends one survival.rest to its required target_endurance, verified by the adapter from the observation; 'sleep_until_rested' sleeps on an observed bed for its optional hours (absent means the adapter's own night), and the sleep adapter's danger refusal reaches the goal typed and unchanged, never retried. 'satisfy_hunger' and 'satisfy_thirst' are served the same deterministic way now — food and water found in known containers, moved to the main inventory, unsafe candidates (rotten, burnt, poisonous, reserved) skipped with recorded reasons, and success only by the observed stat moving. An 'avoid_threat' goal (no parameters — where to retreat to is decided deterministically from the observed threat picture) walks threat-avoiding journeys to the nearest remembered user safe zone or to open ground away from the observed zombies, re-reading the picture every step; it succeeds only on the observed postcondition — nearest zombie at a safe distance, or standing in a safe zone with nothing chasing — and a retreat that cannot open the distance is a typed THREAT_INTERRUPTED naming the nearest observed threat distance.

| Argument | Required | Schema |
| --- | --- | --- |
| `kind` | yes | one of `avoid_threat`, `explore_area`, `learn_recipe`, `loot_area`, `navigate_to`, `read_for_boredom`, `rest_until`, `return_home`, `satisfy_hunger`, `satisfy_thirst`, `sleep_until_rested`, `train_skill`, `treat_wounds` |
| `skill` | no | one of `carpentry`, `cooking`, `electrical`, `farming`, `first_aid`, `fishing`, `foraging`, `mechanics`, `metalworking`, `tailoring`, `trapping` |
| `target_level` | no | `integer`; minimum 1; maximum 10 |
| `satisfy_to` | no | `number`; minimum 0.0; maximum 1.0 |
| `pages` | no | `integer`; minimum 1; maximum 200 |
| `target_x` | no | `integer`; minimum 0; maximum 32000 |
| `target_y` | no | `integer`; minimum 0; maximum 32000 |
| `target_z` | no | `integer`; minimum -32; maximum 31 |
| `scope` | no | one of `building`, `radius`, `room` |
| `radius` | no | `integer`; minimum 1; maximum 30 |
| `take_all` | no | `boolean` |
| `categories` | no | `string`; maxLength 128; comma-joined closed tokens, each of `CLOTHING`, `FOOD`, `LITERATURE`, `MATERIALS`, `MEDICAL`, `OTHER`, `TOOLS`, `WATER`, `WEAPONS` at most once |
| `target_endurance` | for `rest_until` | `number`; minimum 0.05; maximum 1.0 — the rest adapter's own range |
| `hours` | no | `integer`; minimum 1; maximum 12 — the sleep adapter's floor beside the channel's narrower "until rested" ceiling |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 64; pattern `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$` |

`navigate_to` requires all three `target_*` coordinates and takes nothing
else: the sidecar's deterministic route executor walks the character to that
world square — doors, stairs, blocked passages and stuck detection handled
locally, with no model call per square — and the goal ends on the *observed*
arrival or with the executor's typed refusal (`DOOR_LOCKED`,
`DOOR_BARRICADED`, `PATH_NOT_FOUND`, `PATH_STUCK`, `NO_PROGRESS`).

`loot_area` takes only the four loot parameters, all optional: the bare goal
means scope `room`, the useful-only category selection and no `take_all`, and
those defaults are read by the mission, never filled in by this surface. The
sidecar's deterministic loot mission serves it with no model call per
container: it builds the reachable candidate map, opens allowed doors, moves
via the local pathfinder, inspects each container, selects deterministically,
moves items with batch transfers and replans on blockage. Scope semantics:
`room` and `building` are pinned from the observation at activation and need
a build that reports rooms — where the reader is unavailable they are refused
with a typed failure naming `radius` as the alternative, because outdoors and
"no room reader" are deliberately indistinguishable and a guess would loot
the wrong scope; `radius` (a Chebyshev sweep of `radius` squares around the
activation square, requiring `scope: "radius"` beside it) always works. Only
a live session proves a given build reports rooms. The report is honest both
ways: a container skipped for a locked door, a failed path or a budget that
ran out appears in the terminal answer with that reason, never silently
dropped; `take_all` widens the selection to every category but never
overrides the user's reserved items.

`return_home` takes no parameters at all: where home is comes from the
save's memory, set by `pz-agent remember home`, and a spoken or submitted
parameter would be a second definition of home that could disagree with the
remembered one. The sidecar walks it with the same deterministic route
executor `navigate_to` uses — doors, stairs, blocked passages and stuck
detection handled locally — and the goal succeeds only on the *observed*
arrival at the remembered square. No home point readable is a typed
`PRECONDITION_FAILED` whose detail is the remedy verbatim ("stand at home
and run: pz-agent remember home"); a home the map can prove unreachable —
another floor with no remembered stairs, a route sealed by walls — is the
executor's own typed refusal (`PATH_NOT_FOUND`, `DOOR_LOCKED`,
`DOOR_BARRICADED`).

`explore_area` takes only `scope` and `radius`, both optional, and the
absent scope means `radius` — deliberately not loot's `room` default,
because the room the character stands in is the one patch of world already
observed and exploring it is a no-op. The sidecar's deterministic explore
mission serves it with no model call per square: it reads the session's
local map, finds the frontier — unknown squares bordering squares the map
has proven walkable — walks a bounded journey at the nearest one
(deterministic tie-break by coordinates), and lets the observations
gathered on the way sweep squares into the map; a waypoint that becomes
known mid-approach is done without the arrival. Scope semantics are
loot_area's: `room` and `building` are pinned from the observation at
activation and refused with a typed failure naming `radius` where the
reader is unavailable; `radius` (a Chebyshev sweep around the activation
square) always works. The mission ends `complete` when no frontier square
remains in scope — every scope square known or skipped with a recorded
reason — and `no_progress` after three consecutive failed approaches or an
exhausted waypoint budget. The report is honest both ways: `cells_discovered`
is the map's real growth, and every square skipped for a locked door (named
by reference) or a proven no-route appears with that reason, never silently
dropped.

`treat_wounds` takes no parameters at all: which wound first, which dressing,
whether a dirty one may be used and whether a reserved one may be spent are
the deterministic medical policy's decisions, remade against every fresh
observation — a parameter here would be a spoken second opinion on the
triage order. The sidecar's care mission drives it with no model call per
wound: a dressing out of the main inventory is transferred first as its own
observed action, each `medical.bandage` is proven by the adapter (the wound
observed to stop bleeding), and the goal completes only when the newest
observation reports no bleeding wound. Dressings running out with wounds
still bleeding is the typed partial failure (`PRECONDITION_FAILED`) with
the honest count in the detail; the mission's report — parts verified
bandaged, wounds started, bleeding remaining, how it ended — survives the
goal in the sidecar's bounded ledger.

`rest_until` requires `target_endurance` and takes nothing else: the target
is the goal — "rest" without a stated endurance to reach has no
postcondition to verify, and a default would be this channel choosing one
silently. The mission sends one `survival.rest`; the adapter owns the
waiting (its own wall-clock bounds) and the proof (endurance observed at or
above the target, higher than it was). A target the observation already
meets completes without work through the bounded completion probe, and any
adapter refusal rides to the goal typed and unchanged, never retried.

`sleep_until_rested` takes only `hours`, optional: absent means the sleep
adapter's own default night, expressed by omitting the argument rather than
restating the adapter's number, and the ceiling here (12) is deliberately
below the adapter's sixteen-hour maximum — "until rested" is not a request
for the longest sleep the engine allows. The mission names the nearest
observed bed's square deterministically and sends one `survival.sleep`;
sleep is the one action the mod cannot interrupt, so the adapter refuses it
outright while the reflex guard reports any danger, and that refusal
(`POLICY_DENIED`) reaches the goal typed and unchanged — the mission never
re-sends a sleep behind it. Success is the adapter's own evidence: fatigue
observed falling over slept world hours. No bed observed nearby is a typed
`PRECONDITION_FAILED` whose detail is the remedy.

`satisfy_hunger` and `satisfy_thirst` — the channel's founding kinds — are
now served by the same deterministic arrangement rather than by a plan
provider: hunger observed, safe food at hand checked first, otherwise the
remembered containers searched by category, the candidate moved to the main
inventory as its own observed action, and a sensible portion eaten or
drunk. Rotten, burnt, poisonous and unsafe candidates and the user's
strategic reserves are skipped with recorded reasons — the refusal
vocabulary is the consume adapters' own (`NO_SAFE_FOOD`, `NO_SAFE_DRINK`) —
and the goal succeeds only on the observed stat moving. A plan provider may
still *propose* eating on the autonomy loop's initiative path; a goal
submitted here never reaches one.

`avoid_threat` takes no parameters at all: where to retreat *to* is the
deterministic avoid mission's decision from the observed threat picture,
re-read from the current observation at every step — never a snapshot, and
never a spoken or submitted coordinate. The target is the nearest remembered
user safe zone within thirty squares (`pz-agent remember`), or, with none in
range, the square on a bounded ring that maximises the minimum distance to
the observed zombies; the journeys there run with threat-aware routing, so
the route itself detours around remembered sightings and treats a chasing
zombie's square as impassable-preferred (very high cost, never infinite — a
cornered character still gets the least-bad way out). Success is only the
observed postcondition: the newest observation reporting the nearest zombie
at twelve tiles or beyond (twice the threat ladder's reaction range), or the
character standing in a user safe zone with no chasing zombie observed. No
threat observed at activation completes without work through the bounded
completion probe; a retreat that cannot open the distance — cornered,
unroutable, or out of its bounded retreat legs — is a typed
`THREAT_INTERRUPTED` naming the nearest observed threat distance, and the
report (threats at start, nearest before/after, target kind, how it ended)
survives the goal in the sidecar's bounded ledger.

### `pz_goal_status`

The goal channel: which goal is active, what is waiting behind it, and — when 'goal_id' names one — that goal's state, budget and how much of it is left. An id the channel has finished and forgotten is refused rather than answered as 'no such goal', because the two are not the same fact. Three additive keys, each null when there is nothing to say: 'progress' is the deterministic drive's phase (a journey's planning/moving/arrived/refused; a loot sweep's start/approach/open/inspect/transfer; an explore sweep's start/approach; a consume drive's check/fetch/consume/verify; a care drive's start/transfer/treat, start/rest or start/sleep; an avoid drive's start/approach) plus detail-free counters, for the named goal or, with no id, the active one — a goal a plan provider serves has no deterministic phase and honestly answers null; 'paused' is the goal a manual takeover parked, visible until a fresh activation replaces it; 'report' is the named loot or explore goal's ledger, live while the mission runs and sealed after it ends. The phase is the progress-messaging primitive: tell the user about transitions, when the value changes — it moves exactly when the work does, so polling faster buys nothing worth relaying.

| Argument | Required | Schema |
| --- | --- | --- |
| `goal_id` | no | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |

The three additive payload keys in detail:

* `progress` — `{phase, counters}` for the goal the answer is about: the
  named goal when `goal_id` was passed, the active goal otherwise, and only
  while the sidecar's deterministic wrapper is live-driving it. `phase` is a
  closed token minted by the drive itself — `planning`/`moving`/`arrived`/
  `refused` for `navigate_to` and `return_home`, `start`/`approach`/`open`/
  `inspect`/`transfer` for `loot_area`, `start`/`approach` for
  `explore_area`, `check`/`fetch`/`consume`/`verify` for `satisfy_hunger`
  and `satisfy_thirst`, `start`/`transfer`/`treat` for `treat_wounds`,
  `start`/`rest` for `rest_until`, `start`/`sleep` for
  `sleep_until_rested`, `start`/`approach` for `avoid_threat` — and
  `counters` carries the drive's own detail-free
  numbers (`legs_used`; `containers_inspected` and `containers_skipped`;
  `waypoints_visited` and `cells_discovered`; `candidates_tried`,
  `consumed` and `skipped`; `wounds_bandaged` and `bleeding_remaining`;
  `requested` as 0 or 1 for the one-action rest and sleep drives;
  `threats_at_start` and `legs_started` for the retreat drive). A goal
  served by a plan provider has no deterministic phase and answers `null`,
  honestly, as does a goal whose drive already ended. **Clients should
  report progress on phase *transitions*, not on every poll**: the field
  only changes when the work actually moves, which is the whole point of
  publishing it.
* `paused` — the goal a manual takeover parked, as
  `{goal_id, kind, paused_at_ms}` with the loop's one-line reason carried
  under the usual `untrusted_text` quarantine. The queue's own record for
  that goal honestly ended `cancelled` (user input always wins); this marker
  is the other half — paused by the user's own hand, not abandoned — and it
  stays visible until an explicit fresh activation replaces it.
* `report` — the named `loot_area` or `explore_area` goal's full ledger
  document: the live snapshot while the mission runs, the sealed report
  after it ends, for as long as the bounded ledger keeps it (the last few
  missions per kind). Room and building names are redacted at source and
  the document is scrubbed again at this boundary, so every free string in
  it (skip reasons included) arrives under the `untrusted_text` quarantine.
  The consume and care missions keep ledgers of the same shape in the
  sidecar (candidates tried, skip reasons, parts verified bandaged, how the
  mission ended), but this tool does not serve them yet; their terminal
  contract over this surface is the goal record itself — a one-line,
  constants-and-counts summary in `detail` and the observed evidence key
  names on success.

One additive key rides on every *goal payload* (the `goal`, `active` and
`pending[]` objects) rather than beside them:

* `suspended_by` — `null` for every goal that is not currently suspended. A
  pending goal carrying a value is one the sidecar's needs arbiter parked so
  a more urgent need could run first: in `AUTONOMOUS` mode, a need *crossing*
  its critical threshold between observations (hunger or thirst through the
  policy's own critical line, bleeding appearing, danger reaching HIGH with
  nothing chasing) suspends the active goal, injects the matching
  `satisfy_*`/`treat_wounds`/`avoid_threat` goal at the front of the backlog,
  and lets ordinary activation resume the original — wall clock banked, steps
  and mission position intact — once the preemptor reaches any terminal
  state, success or failure alike. The value is the arbiter's deterministic
  token naming the preemptor (`arb.<original-goal-id>.<trigger>.<n>`, also
  the preemptor's idempotency key), shape-checked like every token;
  activation consumes the marker, so a running goal always answers `null`.
  `ASSISTED` sessions never see one — that mode asks, it does not preempt.
  Preemption is bounded by the channel's own suspension cap (three per
  goal); past it the arbiter stands down and the goal runs to its own end.

Over the two-process assembly these keys additionally require the Core
RPC link to carry them; a link whose codec predates them answers `null`
rather than inventing a value.

### `pz_goal_cancel`

Ask for one goal to end. Control, not write: a goal is cancelled *because* something has gone wrong, and gating that on an armed session would make the channel unstoppable by the lever meant to stop it. The channel applies a cancellation on its next tick, so the answer reports the request and the goal's state as it stands and does not claim the goal is already over.

| Argument | Required | Schema |
| --- | --- | --- |
| `goal_id` | yes | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 64; pattern `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$` |

### `pz_safety_stop`

Always available. Clears mod-owned queue entries only, disarms, and works while unarmed, while the planner is absent and while the queue is backed up. Takes no arguments so nothing can make it fail.

No arguments.

### `pz_memory_query`

Known containers, home point, safe zones, failed paths and user reservations. Read-only; returns no secrets and no paths.

| Argument | Required | Schema |
| --- | --- | --- |
| `kinds` | no | array, maxItems 8; items `string`; maxLength 64; pattern `^[a-z][a-z0-9_.\-]{0,63}$` |
| `limit` | no | `integer`; minimum 1; maximum 50; default `20` |

### `pz_debug_doctor`

Full environment report with stable check codes and remediation.

No arguments.

### `pz_debug_tail`

Recent structured log records, redacted and bounded.

| Argument | Required | Schema |
| --- | --- | --- |
| `limit` | no | `integer`; minimum 1; maximum 100; default `20` |
| `level` | no | one of `debug`, `info`, `warning`, `error` |
| `component` | no | `string`; maxLength 64; pattern `^[a-z][a-z0-9_.\-]{0,63}$` |
| `action_id` | no | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |

---

## Capability gating

Eighteen of the 41 tools name a capability. `published_tools()` offers a
tool only when `CapabilityReport.usable()` is true for its capability, which
means `verified` or `available_unverified`; `experimental`, `unsupported` and
`disabled_by_policy` are all unusable. `withheld_tools()` returns the withheld
names with their reasons, and `pz://capabilities` carries them, so a missing
tool is an answer rather than an error.

| Capability | Tools it gates |
| --- | --- |
| `move_to_square` | `pz_action_move_to`, `pz_action_move_near`, `pz_action_open_container` |
| `door_toggle` | `pz_action_open_door`, `pz_action_close_door`, `pz_action_unlock_door` |
| `inventory_transfer` | `pz_action_transfer`, `pz_action_transfer_batch`, `pz_action_ensure_main` |
| `eat_percentage` | `pz_action_eat` |
| `drink_carried` | `pz_action_drink` |
| `drink_world_source` | `pz_action_drink_source` |
| `read_literature` | `pz_action_read` |
| `equipment_equip` | `pz_action_equip` |
| `equipment_unequip` | `pz_action_unequip` |
| `medical_bandage` | `pz_action_bandage` |
| `survival_rest` | `pz_action_rest` |
| `survival_sleep` | `pz_action_sleep` |

The other twenty-three tools name no capability at all. For the three query
tools that is deliberate and documented in `capabilities/probes.py`: everything
`world.inspect`, `container.inspect` and `inventory.search` read is reached
through Java accessors that never appear in the game's Lua, so a probe over
those names would report `unsupported` on a healthy install. They gate on the
observation tier they need instead. It also means they are the three actions
whose availability rests on no runtime evidence.

`survival_sleep` and `drink_world_source` resolve to `experimental` on a clean
static scan, so on most installs `pz_action_sleep` and `pz_action_drink_source`
are **withheld**, with the reason, rather than offered.

---

## Resources

Seven, all `application/json`, all with `subscribable: false`.

| URI | `name` | Content |
| --- | --- | --- |
| `pz://session/current` | `session` | Session id, mode, armed state and protocol version. |
| `pz://observation/latest` | `observation` | Most recent compacted snapshot. |
| `pz://inventory/current` | `inventory` | Container tree with stable refs. |
| `pz://capabilities` | `capabilities` | Probe results and the evidence behind them. |
| `pz://plan/current` | `plan` | Active plan and its step results. |
| `pz://safety/status` | `safety` | Danger level, takeover state and heartbeat health. |
| `pz://diagnostics/recent` | `diagnostics` | Recent diagnostics, redacted. |

Resources are read-only views over state core already holds. Each read carries
the observation `seq` it was built from, which a client uses as an ETag: a
resource that has not moved reports the same `seq`.

**Subscriptions are not delivered.** `subscribable` is `false` on all
seven because the server registers no subscribe handler and nothing in
core publishes resource-change events. A client that could subscribe and was
never notified would read the silence as "nothing has changed", which is the
worst possible failure for the safety view. Poll `pz://safety/status` often;
its own descriptor says so.

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
the answer is the original call's, not a second action. `message` is clipped to
300 characters and `warnings` to 8 entries.

`status: "succeeded"` cannot be constructed without the observed postcondition
under `data.evidence` — the same rule as `ActionResult.succeeded()`, enforced
again where a client reads it.

Neither plan tool ever puts `succeeded` in the envelope `status`: that word is
reserved for a result carrying observed evidence, and a plan record has none to
carry — its steps' evidence was observed by the action engine and stops at the
port. A plan that finished answers `"ok"`, and `data.status` with
`data.terminal` say what it finished as.

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

`reason_code` is from the protocol's closed set (see
[`PROTOCOL.md`](PROTOCOL.md)) and is stable across releases — renaming one is a
protocol major bump. `retryable` reflects `RETRYABLE_CODES`, so a client does not
have to maintain its own table of which failures are worth another attempt.
`diagnostics` is capped at 10 entries.

## What a caller cannot do

- Execute code. There is no field for it anywhere in the surface.
- Name a file. Every IPC filename is a hardcoded constant on both sides.
- Act while disarmed, except through the six `control` tools and the read and
  query ones.
- Choose *what* to eat, drink or read. No tool takes a "pick something" form;
  selection is deterministic policy in `pz_agent_core.policy`. `pz_goal_submit`
  is the same rule one level up: a closed kind set, typed parameters, and no
  free-text field at all — an invented kind is refused, never approximated.
- Pass `allow_windows`. It is not published, because the movement adapter
  refuses it with `POLICY_DENIED`.
- Name a destination for `pz_action_ensure_main`. The only container the adapter
  accepts is the main inventory; any other one is `pz_action_transfer` under a
  different name.
- Use a tool whose capability is not usable. It is not listed and not callable.

See [`SAFETY.md`](SAFETY.md) for where each of those is enforced, and
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for what to do about a refusal.
