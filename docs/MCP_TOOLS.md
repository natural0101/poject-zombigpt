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
| tools | 34 |
| resources | 7 |

`--describe` reports the **whole** catalogue. A running server publishes a
subset of it: a tool whose capability is not usable on the install is withheld.
See *Capability gating* below.

---

## Semantics that apply to every tool

**Every input schema is closed.** `additionalProperties` is `false` on all
34 tools, so an argument this document does not list is rejected rather
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
| `read` | 9 | not required | Answered from state the sidecar already holds |
| `query` | 3 | not required | Submits one of the protocol's `READ_ONLY_ACTIONS`; comes back with an action id, but the character neither moves nor touches anything |
| `write` | 17 | **required** | Changes the world; refused with `NOT_ARMED` on a disarmed session |
| `control` | 5 | not required | Arm, disarm, cancel, stop and the goal verbs — how a disarmed or panicking session is driven |

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
`P3` when the destination changes floor or leaves the safe radius,
`inventory.transfer` becomes `P3` when the source is a world container — and
neither is visible from the tool name, so neither is published.

**Long-running tools return an action id.** `long_running` is `true` for
19 tools. The call returns immediately with an `action_id`; you poll it by
calling again with the same `idempotency_key`, which replays the call and
refreshes it to the action's current state. It does not block the transport and
it does not report success early.

**Idempotency.** Every tool that submits a command takes a required
`idempotency_key`; 22 tools do. Twenty of them accept 1–120 characters with no
further shape; the two goal verbs, `pz_goal_submit` and `pz_goal_cancel`, take
1–64 matching `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$`, because the goal channel
carries the key into its own record and a key is not a place to smuggle text.
Calling twice with the same key does not perform the action twice — the
original result is replayed with `replayed: true`.

**`timeout_ms`** is that one command's lease: integer, 100–300000 ms, default
15000. Nineteen tools take it — every one that submits a single command.
`pz_plan_execute` does not, because a plan is bounded by
`limits.max_real_seconds` instead, and publishing an argument no handler reads
would be a lie shaped like an option.

**No free text.** `pz_plan_execute`'s `goal` (1–200 characters) is the only
field in the whole surface a caller may write in their own words, and it comes
back quarantined. Every other string argument is a ref matching a fixed pattern,
an enum member, or a lower-case token matching
`^[a-z][a-z0-9_.\-]{0,63}$`.

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
| `pz_action_transfer` | write | P1 | yes | yes | `inventory_transfer` |
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

### `pz_action_move_near`

Walk to within interaction range of something in the world. Verified against the object's *re-observed* position, not the one it had when the call was made: an object that is no longer in view cannot be proven to be within arm's reach.

| Argument | Required | Schema |
| --- | --- | --- |
| `object_ref` | yes | `string`; maxLength 220; pattern `^(?:container\|square\|item):[A-Za-z0-9:_.\-]{1,200}$` |
| `radius` | no | `number`; minimum 0.1; maximum 3.0; default `1.5` |
| `max_distance` | no | `integer`; minimum 1; maximum 30; default `20` |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 120 |
| `timeout_ms` | no | `integer`; minimum 100; maximum 300000; default `15000` |

### `pz_action_open_container`

Get within reach of a world container, so its contents can be taken. Its name reads like a query and it is not one: this walks the character across a room, so it needs an armed session like any other move. A door in the way is not opened — that is a different object and a different action.

| Argument | Required | Schema |
| --- | --- | --- |
| `container_ref` | yes | `string`; maxLength 220; pattern `^container:[A-Za-z0-9:_.\-]{1,200}$` |
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

Ask the typed goal channel for one of the things it carries. The kind set is closed and there is no free-text field at all: an invented kind is refused, never approximated. The channel admits the goal to a bounded backlog and answers with its id and state — 'pending' is the honest word for a goal nothing has started yet, and every goal carries a wall-clock, step and time-to-live budget so that it reaches a terminal state whether or not it is served. Which sandwich satisfies a hunger goal is never decided here.

| Argument | Required | Schema |
| --- | --- | --- |
| `kind` | yes | one of `learn_recipe`, `read_for_boredom`, `satisfy_hunger`, `satisfy_thirst`, `train_skill` |
| `skill` | no | one of `carpentry`, `cooking`, `electrical`, `farming`, `first_aid`, `fishing`, `foraging`, `mechanics`, `metalworking`, `tailoring`, `trapping` |
| `target_level` | no | `integer`; minimum 1; maximum 10 |
| `satisfy_to` | no | `number`; minimum 0.0; maximum 1.0 |
| `pages` | no | `integer`; minimum 1; maximum 200 |
| `idempotency_key` | yes | `string`; minLength 1; maxLength 64; pattern `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$` |

### `pz_goal_status`

The goal channel: which goal is active, what is waiting behind it, and — when 'goal_id' names one — that goal's state, budget and how much of it is left. An id the channel has finished and forgotten is refused rather than answered as 'no such goal', because the two are not the same fact.

| Argument | Required | Schema |
| --- | --- | --- |
| `goal_id` | no | `string`; maxLength 36; pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |

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

Fourteen of the 34 tools name a capability. `published_tools()` offers a
tool only when `CapabilityReport.usable()` is true for its capability, which
means `verified` or `available_unverified`; `experimental`, `unsupported` and
`disabled_by_policy` are all unusable. `withheld_tools()` returns the withheld
names with their reasons, and `pz://capabilities` carries them, so a missing
tool is an answer rather than an error.

| Capability | Tools it gates |
| --- | --- |
| `move_to_square` | `pz_action_move_to`, `pz_action_move_near`, `pz_action_open_container` |
| `inventory_transfer` | `pz_action_transfer`, `pz_action_ensure_main` |
| `eat_percentage` | `pz_action_eat` |
| `drink_carried` | `pz_action_drink` |
| `drink_world_source` | `pz_action_drink_source` |
| `read_literature` | `pz_action_read` |
| `equipment_equip` | `pz_action_equip` |
| `equipment_unequip` | `pz_action_unequip` |
| `medical_bandage` | `pz_action_bandage` |
| `survival_rest` | `pz_action_rest` |
| `survival_sleep` | `pz_action_sleep` |

The other twenty tools name no capability at all. For the three query
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
- Act while disarmed, except through the five `control` tools and the read and
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
