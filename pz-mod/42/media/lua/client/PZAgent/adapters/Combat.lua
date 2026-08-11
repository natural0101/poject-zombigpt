--[[
PZAgent.Adapters.Combat -- the ASSISTED combat rung: combat.equip_best,
combat.shove, combat.engage and combat.retreat, all on the `combat_assist`
capability.

This file is NOT autonomous_attack. That capability's ceiling is "unsupported
by design" (§12.4) and nothing here touches its probe or its pin: every command
below arrives only as an explicit user-submitted goal or tool call, is one
bounded window, and ends by re-observation. There is no "attack until dead"
loop anywhere in this file -- combat.engage is a single window of at most
MAX_SWINGS swings inside ENGAGE_WINDOW_MS, terminal when the window closes,
and the mission issues the next window (or a retreat) as its own command, which
is exactly the seam where safety.stop and the reflex guard interrupt.

Invariants, in the repository's usual order of importance:

* **The postcondition never trusts the swing.** A shove or attack press
  answers nothing useful, so `succeeded` is minted only off the re-observed
  target: prone, dead, or honestly absent from the loaded world. A press that
  was made while the target still stands is POSTCONDITION_FAILED with the
  honest picture, never a success.
* **No system input.** The presses below are guarded engine methods on the
  player object -- the same call surface every other adapter uses -- and their
  spellings are the least certain rows in docs/GAME_API_VERIFICATION.md, which
  says so. A build without them costs CAPABILITY_UNAVAILABLE naming the
  symbol, never a keystroke fallback.
* **Interruption is consulted at the top of every prepare, begin and progress**
  through Toolkit.interruption, so a takeover or a reflex-guard rung stops a
  window mid-poll, and the between-window seam stops the fight entirely.
* **The mod checks only what it can observe locally.** Group size, endurance,
  fatigue and injury gates are the deterministic policy's on the Python side;
  duplicating them here would let the two disagree. What this file gates on is
  what only it can see: the weapon in the hand, its readable condition, the
  target within reach, the engine symbols present.

The retreat walk duplicates Movement's small enqueueWalk pattern rather than
importing Movement: adapters do not depend on each other, and the pattern is
two guarded calls. Its door handling is the same bounded rescue idea --
attempted only when the walk is already failing, only on a door that reads
closed, unlocked and unbarricaded, verified by re-read, at most RETREAT_DOORS
attempts -- and with allow_doors=false it does nothing at all.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walks
-- adapters/ in an order this file does not control. The statement form is
-- deliberate -- the paren form is banned as dynamic loading -- and the test
-- harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Combat = {}
PZAgent.Adapters.Combat = Combat

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

-- ---------------------------------------------------------------------------
-- bounds
-- ---------------------------------------------------------------------------

--- Zombies one resolution scan may read, the same cap the observation uses.
Combat.MAX_ZOMBIE_SCAN = 256

--- One engage window, in milliseconds. The window is bounded by swings AND by
--- this clock: whichever closes first ends the command, and the declared
--- timeout below is this same number so the runtime's lease agrees.
Combat.ENGAGE_WINDOW_MS = 4000

--- How long a shove is given to visibly land before the command fails.
Combat.SHOVE_WINDOW_MS = 3000

--- Swings one engage window may press. The argument is clamped into this
--- range; the default is deliberately below the maximum.
Combat.MIN_SWINGS = 1
Combat.MAX_SWINGS = 3
Combat.DEFAULT_SWINGS = 2

--- Melee interaction range: the shared reach plus one square, which is the
--- "reach+1" the engage precondition promises. Anything farther is a walk the
--- mission plans, never a lunge this adapter improvises.
Combat.MELEE_REACH = toolkit().DEFAULT_REACH + 1

--- A shove counts as landed when the target's distance grew by at least this.
Combat.PUSH_EPSILON = 0.3

--- Retreat distances, in squares. Bounded both ways: the floor keeps the
--- command from being a no-op shuffle, the ceiling keeps one command from
--- being a cross-map flight.
Combat.RETREAT_MIN = 3
Combat.RETREAT_MAX = 15
Combat.RETREAT_DEFAULT = 8

--- The nearest zombie must end at least this much farther for retreat to
--- claim success; below it the reading is noise, not ground gained.
Combat.RETREAT_EPSILON = 0.5

--- Door-opening attempts one retreat may spend, counted per attempt exactly
--- as Movement counts its own.
Combat.RETREAT_DOORS = 2

--- Game ticks the equip action is given -- the same pacing Equipment uses.
Combat.EQUIP_TIME = 50

Combat.EQUIP_TIMEOUT_MS = 15000
Combat.SHOVE_TIMEOUT_MS = 8000
Combat.RETREAT_TIMEOUT_MS = 30000
Combat.POLL_MS = 150

--- How the refusals name the probed symbols: one spelling on the wire per
--- surface, whichever member was actually looked for. The shove and attack
--- spellings are the least certain in the whole mod -- see the combat section
--- of docs/GAME_API_VERIFICATION.md, which records every candidate.
Combat.SHOVE_SYMBOL = "IsoPlayer.setForceShove / setDoShove / DoShove"
Combat.ATTACK_SYMBOL = "IsoPlayer.pressAttack / DoAttack"
Combat.CONDITION_SYMBOL = "InventoryItem.getCondition"
Combat.ZOMBIE_LIST_SYMBOL = "IsoCell.getZombieList"

--- Candidate spellings for the game's own input presses, probed in order.
--- Each carries the argument its assumed signature wants; a Lua double simply
--- ignores an extra argument, and the real signatures are requires_live.
local SHOVE_PRESSES = {
  { name = "setForceShove", arg = true },
  { name = "setDoShove", arg = true },
  { name = "DoShove" },
}

local ATTACK_PRESSES = {
  { name = "pressAttack", arg = true },
  { name = "DoAttack", arg = 0 },
}

--- Facing spellings. Failure to face is recorded, never fatal: a whiffed
--- swing fails on the postcondition, which is the honest place.
local FACE_NAMES = { "faceThisObject", "FaceThisObject" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG for the usual load-order reason; the
--- dispatcher refuses an unknown declared type at registration.
local ARG = { NUMBER = "number", REF = "ref", BOOLEAN = "boolean" }

local ZOMBIE_KIND = { [PZAgent.Refs.KIND.ZOMBIE] = true }

-- ---------------------------------------------------------------------------
-- reading zombies
-- ---------------------------------------------------------------------------

--- The cell's zombie list plus its size, or nil and a refusal naming the gap.
local function zombieList()
  local Toolkit = toolkit()
  if type(getCell) ~= "function" then
    return Toolkit.unavailable("getCell")
  end
  local ok, cell = pcall(getCell)
  if not ok or cell == nil then
    return Toolkit.unavailable("getCell")
  end
  local okList, list = Toolkit.call(cell, "getZombieList")
  if not okList or list == nil then
    return Toolkit.unavailable(Combat.ZOMBIE_LIST_SYMBOL)
  end
  local size = Toolkit.listSize(list)
  if size == nil then
    return Toolkit.unavailable(Combat.ZOMBIE_LIST_SYMBOL .. "().size")
  end
  return list, size
end

--- The zombie carrying `runtimeId`, `false` when the bounded scan finished
--- without it, or nil plus a refusal when the list could not be read at all.
--- The three answers stay three: "not there" is an observation the
--- postconditions build on, and an unreadable list must never become it.
local function findZombie(runtimeId)
  local Toolkit = toolkit()
  local list, sizeOrCode, detail = zombieList()
  if list == nil then
    return nil, sizeOrCode, detail
  end
  local scanned = math.min(sizeOrCode, Combat.MAX_ZOMBIE_SCAN)
  for index = 0, scanned - 1 do
    local zombie = Toolkit.listGet(list, index)
    if zombie ~= nil and Toolkit.readIdentity(zombie, { "getOnlineID", "getID" }) == runtimeId then
      return zombie
    end
  end
  return false
end

Combat.findZombie = findZombie

local function pointOf(zombie)
  local Toolkit = toolkit()
  local x = Toolkit.readNumberOf(zombie, { "getX" })
  local y = Toolkit.readNumberOf(zombie, { "getY" })
  local z = Toolkit.readNumberOf(zombie, { "getZ" })
  if x == nil or y == nil or z == nil then
    return nil
  end
  return { x = x, y = y, z = math.floor(z) }
end

--- The target's body state as the same tri-state token Observe emits: "prone"
--- when a floor reader answered true, "crawling" when the crawl reader did,
--- "standing" only when at least one reader answered, nil when none did.
--- Absent never reads as standing -- and, load-bearingly for the engage
--- postcondition, absent never reads as prone either.
local function stateOf(zombie)
  local Toolkit = toolkit()
  local prone = Toolkit.readBooleanOf(zombie, { "isOnFloor", "isProne" })
  local crawling = Toolkit.readBooleanOf(zombie, { "isCrawling" })
  if prone == nil and crawling == nil then
    return nil
  end
  if prone == true then
    return "prone"
  end
  if crawling == true then
    return "crawling"
  end
  return "standing"
end

Combat.stateOf = stateOf

local function deadOf(zombie)
  return toolkit().readBooleanOf(zombie, { "isDead" })
end

--- Down enough for a window to end: prone on the floor, or positively dead.
local function targetDown(zombie)
  return stateOf(zombie) == "prone" or deadOf(zombie) == true
end

--- Resolve the target reference into spec fields both target commands share.
local function targetSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local ref, refCode, refDetail = Toolkit.readRef(args, "target_ref", PZAgent.Refs.KIND.ZOMBIE, ctx)
  if ref == nil then
    return nil, refCode, refDetail
  end
  local parsed, parseError = PZAgent.Refs.parseZombie(ref)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local identity = tonumber(parsed.runtime_id)
  if identity == nil then
    return nil, reasons.INVALID_REF, "the zombie reference carries no numeric identity"
  end
  return { target_ref = ref, identity = identity }
end

--- The live target, its position and its plane distance from the character --
--- or a refusal. Reused by validate (the reach gate) and by begin (a fresh
--- handle; the one validate held may be a tick stale).
local function resolveTarget(ctx, identity)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local zombie, code, detail = findZombie(identity)
  if zombie == nil then
    return nil, code, detail
  end
  if zombie == false then
    return nil, reasons.PRECONDITION_FAILED, "the target zombie is no longer observed in the loaded world"
  end
  local point = pointOf(zombie)
  if point == nil then
    return nil, reasons.PRECONDITION_FAILED, "the target reports no position"
  end
  local snapshot = Toolkit.snapshot(ctx)
  if type(snapshot.position) ~= "table" then
    return nil, reasons.PRECONDITION_FAILED, "the character reports no position"
  end
  if snapshot.position.z ~= point.z then
    return nil, reasons.TARGET_OUT_OF_RANGE, "the target stands on another floor"
  end
  return zombie, nil, nil, point, Toolkit.planeDistance(snapshot.position, point.x, point.y)
end

-- ---------------------------------------------------------------------------
-- the presses
-- ---------------------------------------------------------------------------

local function pressAvailable(player, presses)
  local Toolkit = toolkit()
  for index = 1, #presses do
    if Toolkit.method(player, presses[index].name) ~= nil then
      return true
    end
  end
  return false
end

--- Make the game's own input press through the first spelling this build has.
--- The press returning is never treated as the blow landing; verify re-reads.
local function press(ctx, presses, symbol)
  local Toolkit = toolkit()
  for index = 1, #presses do
    local candidate = presses[index]
    if Toolkit.method(ctx.player, candidate.name) ~= nil then
      local ok, err
      if candidate.arg ~= nil then
        ok, err = Toolkit.call(ctx.player, candidate.name, candidate.arg)
      else
        ok, err = Toolkit.call(ctx.player, candidate.name)
      end
      if not ok then
        return nil, Toolkit.reasons().QUEUE_REJECTED, string.format("the press failed: %s", tostring(err))
      end
      return true
    end
  end
  return Toolkit.unavailable(symbol)
end

--- Turn toward the target, guarded. Returns whether a facing call took;
--- absence is recorded in the evidence, never fatal -- a swing made facing the
--- wrong way fails on the re-observation, which is the honest place.
local function faceTarget(ctx, zombie)
  local Toolkit = toolkit()
  for index = 1, #FACE_NAMES do
    if Toolkit.method(ctx.player, FACE_NAMES[index]) ~= nil then
      local ok = Toolkit.call(ctx.player, FACE_NAMES[index], zombie)
      return ok == true
    end
  end
  return false
end

-- ---------------------------------------------------------------------------
-- combat.equip_best
-- ---------------------------------------------------------------------------

local EQUIP_REQUIRES = { "ISTimedActionQueue.add", "ISEquipWeaponAction", "ISEquipWeaponAction.new" }

--- Is this item a weapon as far as this build will say? The engine's own
--- class test first; the display category second, matched case-insensitively
--- against the one word the game uses.
local function isWeapon(item)
  local Toolkit = toolkit()
  if type(instanceof) == "function" then
    local ok, result = pcall(instanceof, item, "HandWeapon")
    if ok and result == true then
      return true
    end
  end
  local category = Toolkit.readStringOf(item, { "getDisplayCategory", "getCategory" })
  return type(category) == "string" and category:lower() == "weapon"
end

Combat.isWeapon = isWeapon

--- The ranking record for one carried weapon, or nil when it is excluded.
---
--- Exclusion is exactly "no readable condition above zero": a weapon whose
--- wear cannot be read must not be ranked as if it were pristine, and a
--- broken one is not a weapon. The rank itself is deterministic and
--- documented once, here: condition fraction (condition / condition_max)
--- descending; a weapon whose max is unreadable keeps fraction 0, so any
--- measurable weapon outranks it while it stays eligible as a last resort;
--- then weight ascending (unreadable weight sorts last); then runtime id
--- ascending so two identical weapons pick the same one every time.
local function rankOf(item)
  local Toolkit = toolkit()
  local condition = Toolkit.readNumberOf(item, { "getCondition" })
  if condition == nil or condition <= 0 then
    return nil
  end
  local identity = Toolkit.readIdentity(item, { "getID" })
  if identity == nil then
    return nil
  end
  local conditionMax = Toolkit.readNumberOf(item, { "getConditionMax" })
  local fraction = 0
  if conditionMax ~= nil and conditionMax > 0 then
    fraction = condition / conditionMax
  end
  return {
    identity = identity,
    condition = condition,
    condition_max = conditionMax,
    fraction = fraction,
    weight = Toolkit.readNumberOf(item, { "getUnequippedWeight", "getActualWeight", "getWeight" }) or math.huge,
  }
end

local function outranks(candidate, best)
  if best == nil then
    return true
  end
  if candidate.fraction ~= best.fraction then
    return candidate.fraction > best.fraction
  end
  if candidate.weight ~= best.weight then
    return candidate.weight < best.weight
  end
  return candidate.identity < best.identity
end

--- The best carried weapon by the documented ranking, with its rank record.
function Combat.bestWeapon(ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local inventory, code, detail = Toolkit.playerInventory(ctx.player)
  if inventory == nil then
    return nil, code, detail
  end
  local items = Toolkit.containerItems(inventory)
  if items == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  local best, bestRank = nil, nil
  local considered = 0
  for index = 1, #items do
    local item = items[index]
    if isWeapon(item) then
      considered = considered + 1
      local rank = rankOf(item)
      if rank ~= nil and outranks(rank, bestRank) then
        best, bestRank = item, rank
      end
    end
  end
  if best == nil then
    return nil, reasons.PRECONDITION_FAILED, "no usable weapon is carried"
  end
  bestRank.considered = considered
  return best, nil, nil, bestRank
end

--- The carried item with `identity`, re-resolved fresh from the inventory.
local function carriedByIdentity(ctx, identity)
  local Toolkit = toolkit()
  local inventory, code, detail = Toolkit.playerInventory(ctx.player)
  if inventory == nil then
    return nil, code, detail
  end
  local count, found = Toolkit.countIdentity(inventory, identity)
  if count == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  if found == nil then
    return nil, Toolkit.reasons().PRECONDITION_FAILED, "the chosen weapon is no longer carried"
  end
  return found
end

local EquipBest = nil

local function equipBestValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(EquipBest.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, {}, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local weapon, code, detail, rank = Combat.bestWeapon(ctx)
  if weapon == nil then
    return nil, code, detail
  end
  local snapshot = Toolkit.snapshot(ctx)
  rank.unchanged = type(snapshot.hands) == "table" and snapshot.hands.primary == rank.identity
  Toolkit.state(ctx).equip_best = rank
  return true
end

local function equipBestBegin(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).equip_best
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no chosen weapon"
  end
  if spec.unchanged then
    -- The best weapon is already in the hand: nothing to queue, only a
    -- re-observation for verify to make.
    return "done"
  end
  local item, itemCode, itemDetail = carriedByIdentity(ctx, spec.identity)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local twoHands = Toolkit.readBooleanOf(item, { "isTwoHandWeapon" }) == true
  local action, actionCode, actionDetail =
    Toolkit.construct("ISEquipWeaponAction", ctx.player, item, Combat.EQUIP_TIME, true, twoHands)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

local function equipBestVerify(_, before, after, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).equip_best
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no chosen weapon"
  end
  if type(after) ~= "table" or type(after.hands) ~= "table" or after.hands.primary ~= spec.identity then
    return nil, reasons.POSTCONDITION_FAILED, "the primary hand does not hold the chosen weapon"
  end
  local evidence = {
    kind = "best_weapon_equipped",
    runtime_id = spec.identity,
    condition = spec.condition,
    condition_max = spec.condition_max,
    weapons_considered = spec.considered,
    primary_after = after.hands.primary,
  }
  if spec.unchanged then
    evidence.unchanged_is_success = true
  else
    evidence.primary_before = type(before) == "table" and type(before.hands) == "table" and before.hands.primary
      or nil
  end
  return evidence
end

EquipBest = toolkit().declare({
  name = "combat.equip_best",
  capability = toolkit().CAPABILITY.COMBAT_ASSIST,
  -- Experimental across all four: the shove and attack press spellings are the
  -- least certain engine assumptions in the mod, and the report must say so
  -- rather than publish the ordinary unverified state.
  experimental = true,
  requires = EQUIP_REQUIRES,
  timeout_ms = Combat.EQUIP_TIMEOUT_MS,
  poll_interval_ms = Combat.POLL_MS,
  args = {},
  validate = equipBestValidate,
  begin = equipBestBegin,
  verify = equipBestVerify,
})

Combat.EquipBest = EquipBest
toolkit().register(EquipBest)

-- ---------------------------------------------------------------------------
-- combat.shove
-- ---------------------------------------------------------------------------

local SHOVE_ARGS = { "target_ref" }

local Shove = nil

local function shoveValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Shove.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, SHOVE_ARGS, { "target_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local spec, specCode, specDetail = targetSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  if not pressAvailable(ctx.player, SHOVE_PRESSES) then
    return Toolkit.unavailable(Combat.SHOVE_SYMBOL)
  end
  local zombie, code, detail, _, distance = resolveTarget(ctx, spec.identity)
  if zombie == nil then
    return nil, code, detail
  end
  if distance > Combat.MELEE_REACH then
    return nil,
      reasons.TARGET_OUT_OF_RANGE,
      string.format("the target is %.2f squares away, past the %.1f a shove can reach", distance, Combat.MELEE_REACH)
  end
  spec.distance_before = distance
  spec.state_before = stateOf(zombie)
  Toolkit.state(ctx).shove = spec
  return true
end

local function shoveBegin(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).shove
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no target"
  end
  local zombie, targetCode, targetDetail = resolveTarget(ctx, spec.identity)
  if zombie == nil then
    return nil, targetCode, targetDetail
  end
  spec.faced = faceTarget(ctx, zombie)
  local pressed, pressCode, pressDetail = press(ctx, SHOVE_PRESSES, Combat.SHOVE_SYMBOL)
  if pressed == nil then
    return nil, pressCode, pressDetail
  end
  spec.started_ms = ctx.now_ms or 0
  return true
end

--- One shove observation: gone, felled, pushed back, still standing -- or a
--- refusal when the world cannot be read. Shared by progress and verify so
--- the two cannot disagree about what counts.
local function shoveReading(spec)
  local Toolkit = toolkit()
  local zombie, code, detail = findZombie(spec.identity)
  if zombie == nil then
    return nil, code, detail
  end
  if zombie == false then
    return { gone = true }
  end
  local reading = { state = stateOf(zombie) }
  local point = pointOf(zombie)
  if point ~= nil and type(spec.player_position) == "table" then
    reading.distance = Toolkit.planeDistance(spec.player_position, point.x, point.y)
  end
  return reading
end

local function shoveObserved(ctx, spec)
  local Toolkit = toolkit()
  local snapshot = Toolkit.snapshot(ctx)
  spec.player_position = snapshot.position
  return shoveReading(spec)
end

local function shoveProgress(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).shove
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no target"
  end
  local reading, readCode, readDetail = shoveObserved(ctx, spec)
  if reading == nil then
    return nil, readCode, readDetail
  end
  if reading.gone or reading.state == "prone" then
    return "done"
  end
  if reading.distance ~= nil and reading.distance > spec.distance_before + Combat.PUSH_EPSILON then
    return "done"
  end
  local elapsed = (ctx.now_ms or 0) - (spec.started_ms or 0)
  if elapsed >= Combat.SHOVE_WINDOW_MS then
    -- The window closed. "done" hands the decision to verify, which will read
    -- the target one more time and fail honestly if nothing moved.
    return "done"
  end
  return "running"
end

local function shoveVerify(_, _, _, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).shove
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no target"
  end
  local reading, readCode, readDetail = shoveObserved(ctx, spec)
  if reading == nil then
    return nil, readCode, readDetail
  end
  local evidence = {
    kind = "target_shoved",
    target_ref = spec.target_ref,
    faced = spec.faced == true,
  }
  if reading.gone then
    -- The honest absence: the target is no longer observed in the loaded
    -- world, named as exactly that rather than as a state nobody read.
    evidence.target_gone = true
    evidence.distance_before = spec.distance_before
    return evidence
  end
  if reading.state == "prone" then
    evidence.target_state_after = "prone"
    evidence.state_before = spec.state_before
    evidence.state_after = "prone"
    return evidence
  end
  if reading.distance ~= nil and reading.distance > spec.distance_before + Combat.PUSH_EPSILON then
    evidence.pushed_back = true
    evidence.distance_before = spec.distance_before
    evidence.distance_after = reading.distance
    return evidence
  end
  return nil,
    reasons.POSTCONDITION_FAILED,
    string.format(
      "the target was neither felled nor pushed back: it reads %s at %.2f squares (was %.2f)",
      reading.state or "state-unreadable",
      reading.distance or spec.distance_before,
      spec.distance_before
    )
end

Shove = toolkit().declare({
  name = "combat.shove",
  capability = toolkit().CAPABILITY.COMBAT_ASSIST,
  experimental = true,
  requires = { "getCell" },
  timeout_ms = Combat.SHOVE_TIMEOUT_MS,
  poll_interval_ms = Combat.POLL_MS,
  args = {
    target_ref = { type = ARG.REF, required = true, kinds = ZOMBIE_KIND },
  },
  validate = shoveValidate,
  begin = shoveBegin,
  progress = shoveProgress,
  verify = shoveVerify,
})

Combat.Shove = Shove
toolkit().register(Shove)

-- ---------------------------------------------------------------------------
-- combat.engage
-- ---------------------------------------------------------------------------

local ENGAGE_ARGS = { "target_ref", "max_swings" }

local Engage = nil

local function engageValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Engage.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, ENGAGE_ARGS, { "target_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local spec, specCode, specDetail = targetSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local maxSwings, swingsCode, swingsDetail = Toolkit.readCount(args, "max_swings", {
    default = Combat.DEFAULT_SWINGS,
    minimum = Combat.MIN_SWINGS,
    maximum = Combat.MAX_SWINGS,
  })
  if maxSwings == nil then
    return nil, swingsCode, swingsDetail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  -- The weapon gate this side owns: a weapon in the hand with a readable,
  -- positive condition. Everything else the policy gates (group size,
  -- endurance, fatigue, injury) is deterministic Python and is deliberately
  -- not restated here.
  local okHand, weapon = Toolkit.call(ctx.player, "getPrimaryHandItem")
  if not okHand then
    return Toolkit.unavailable("IsoPlayer.getPrimaryHandItem")
  end
  if weapon == nil then
    return nil, reasons.PRECONDITION_FAILED, "no weapon is equipped in the primary hand"
  end
  local condition = Toolkit.readNumberOf(weapon, { "getCondition" })
  if condition == nil then
    -- The reader is absent, which is a different fact from a broken weapon
    -- and must be named as the capability gap it is.
    return Toolkit.unavailable(Combat.CONDITION_SYMBOL)
  end
  if condition <= 0 then
    return nil, reasons.PRECONDITION_FAILED, "the equipped weapon is broken"
  end
  if not pressAvailable(ctx.player, ATTACK_PRESSES) then
    return Toolkit.unavailable(Combat.ATTACK_SYMBOL)
  end
  local zombie, code, detail, _, distance = resolveTarget(ctx, spec.identity)
  if zombie == nil then
    return nil, code, detail
  end
  if distance > Combat.MELEE_REACH then
    return nil,
      reasons.TARGET_OUT_OF_RANGE,
      string.format(
        "the target is %.2f squares away, past the %.1f one window reaches",
        distance,
        Combat.MELEE_REACH
      )
  end
  spec.max_swings = maxSwings
  spec.swings = 0
  spec.distance_before = distance
  spec.state_before = stateOf(zombie)
  spec.weapon_condition = condition
  Toolkit.state(ctx).engage = spec
  return true
end

local function engageBegin(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).engage
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no target"
  end
  local zombie, targetCode, targetDetail = resolveTarget(ctx, spec.identity)
  if zombie == nil then
    return nil, targetCode, targetDetail
  end
  spec.faced = faceTarget(ctx, zombie)
  local pressed, pressCode, pressDetail = press(ctx, ATTACK_PRESSES, Combat.ATTACK_SYMBOL)
  if pressed == nil then
    return nil, pressCode, pressDetail
  end
  spec.swings = 1
  spec.started_ms = ctx.now_ms or 0
  return true
end

--- One poll of the window. Interruption first -- safety.stop and the reflex
--- guard land here mid-window, and between windows each new command is its
--- own gate. The window closes on an observed outcome, on the swing budget
--- having been spent and the clock running out, or on the clock alone;
--- closing is always "done", and whether "done" is a success is verify's
--- question against the re-observed target, never this function's.
local function engageProgress(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).engage
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no target"
  end
  local zombie, findCode, findDetail = findZombie(spec.identity)
  if zombie == nil then
    return nil, findCode, findDetail
  end
  if zombie == false or targetDown(zombie) then
    return "done"
  end
  local elapsed = (ctx.now_ms or 0) - (spec.started_ms or 0)
  if elapsed >= Combat.ENGAGE_WINDOW_MS then
    return "done"
  end
  if spec.swings < spec.max_swings then
    local pressed, pressCode, pressDetail = press(ctx, ATTACK_PRESSES, Combat.ATTACK_SYMBOL)
    if pressed == nil then
      return nil, pressCode, pressDetail
    end
    spec.swings = spec.swings + 1
  end
  return "running"
end

local function engageVerify(_, _, _, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).engage
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no target"
  end
  local zombie, code, detail = findZombie(spec.identity)
  if zombie == nil then
    return nil, code, detail
  end
  local evidence = {
    kind = "engage_window",
    target_ref = spec.target_ref,
    swings_attempted = spec.swings,
    faced = spec.faced == true,
  }
  if zombie == false then
    evidence.target_gone = true
    return evidence
  end
  if deadOf(zombie) == true then
    evidence.target_state_after = "dead"
    return evidence
  end
  local state = stateOf(zombie)
  if state == "prone" then
    evidence.target_state_after = "prone"
    return evidence
  end
  -- The anti-lie branch: the attack was pressed and the target still stands.
  -- The honest picture goes on the failure; the mission decides whether the
  -- next command is another window or a retreat.
  local snapshot = Toolkit.snapshot(ctx)
  local distance = nil
  local point = pointOf(zombie)
  if point ~= nil and type(snapshot.position) == "table" then
    distance = Toolkit.planeDistance(snapshot.position, point.x, point.y)
  end
  return nil,
    reasons.POSTCONDITION_FAILED,
    string.format(
      "after %d swing(s) the target still reads %s at %s squares",
      spec.swings,
      state or "state-unreadable",
      distance ~= nil and string.format("%.2f", distance) or "an unreadable distance"
    )
end

Engage = toolkit().declare({
  name = "combat.engage",
  capability = toolkit().CAPABILITY.COMBAT_ASSIST,
  experimental = true,
  requires = { "getCell" },
  -- The declared timeout IS the window: the runtime's lease closes the
  -- command at the same clock the progress step watches.
  timeout_ms = Combat.ENGAGE_WINDOW_MS,
  poll_interval_ms = Combat.POLL_MS,
  args = {
    target_ref = { type = ARG.REF, required = true, kinds = ZOMBIE_KIND },
    max_swings = {
      type = ARG.NUMBER,
      integer = true,
      min = Combat.MIN_SWINGS,
      max = Combat.MAX_SWINGS,
      default = Combat.DEFAULT_SWINGS,
    },
  },
  validate = engageValidate,
  begin = engageBegin,
  progress = engageProgress,
  verify = engageVerify,
})

Combat.Engage = Engage
toolkit().register(Engage)

-- ---------------------------------------------------------------------------
-- combat.retreat
-- ---------------------------------------------------------------------------

local RETREAT_ARGS = { "distance", "allow_doors" }
local RETREAT_REQUIRES = { "getCell", "ISWalkToTimedAction", "ISWalkToTimedAction.new", "ISTimedActionQueue.add" }

--- Within this of the retreat point, on its floor, counts as arrived -- the
--- same half-diagonal clearance Movement's default radius exists for.
Combat.RETREAT_ARRIVE = 0.75

--- The nearest same-floor zombie: `{ point = ... }, distance`, `false` when
--- none is observed, or nil plus a refusal. Same-floor only, deliberately: a
--- horde one storey up neither picks the direction nor blocks the claim that
--- ground was gained. Ties keep the first in engine order, which is as
--- deterministic as the engine's list.
local function nearestZombie(position)
  local Toolkit = toolkit()
  local list, sizeOrCode, detail = zombieList()
  if list == nil then
    return nil, sizeOrCode, detail
  end
  local scanned = math.min(sizeOrCode, Combat.MAX_ZOMBIE_SCAN)
  local nearest, nearestDistance = nil, nil
  for index = 0, scanned - 1 do
    local zombie = Toolkit.listGet(list, index)
    if zombie ~= nil then
      local point = pointOf(zombie)
      if point ~= nil and point.z == position.z then
        local distance = Toolkit.planeDistance(position, point.x, point.y)
        if nearestDistance == nil or distance < nearestDistance then
          nearest, nearestDistance = { point = point }, distance
        end
      end
    end
  end
  if nearest == nil then
    return false
  end
  return nearest, nearestDistance
end

Combat.nearestZombie = nearestZombie

--- The unit away-vector from the threat, deterministic in every case: when
--- the threat stands on the character's exact point there is no direction to
--- read, so east is chosen -- a fixed, documented pick rather than whatever
--- the engine's iteration order happens to yield.
local function awayVector(position, threat)
  local dx = position.x - threat.x
  local dy = position.y - threat.y
  local norm = math.sqrt((dx * dx) + (dy * dy))
  if norm == 0 then
    return 1, 0
  end
  return dx / norm, dy / norm
end

Combat.awayVector = awayVector

--- Queue one tagged walk to the retreat square. The small enqueueWalk
--- pattern, duplicated from Movement on purpose: adapters do not import each
--- other, and the pattern is two guarded calls.
local function enqueueRetreatWalk(ctx, spec)
  local Toolkit = toolkit()
  local square, missing = Toolkit.gridSquare(spec.goal.x, spec.goal.y, spec.goal.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, Toolkit.reasons().TARGET_NOT_LOADED, "the retreat square is no longer loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local action, code, detail = Toolkit.construct("ISWalkToTimedAction", ctx.player, square)
  if action == nil then
    return nil, code, detail
  end
  return Toolkit.enqueue(ctx, action)
end

--- Movement's door-shape test, restated: the engine's own class test first,
--- then the lock-beside-open accessor pair that separates a door from a
--- window.
local function isDoorObject(object)
  local Toolkit = toolkit()
  if type(instanceof) == "function" then
    local ok, result = pcall(instanceof, object, "IsoDoor")
    if ok and result == true then
      return true
    end
  end
  return Toolkit.method(object, "isLocked") ~= nil and Toolkit.method(object, "IsOpen") ~= nil
end

local DOOR_SCAN = {
  { 0, 0 },
  { 1, 0 },
  { -1, 0 },
  { 0, 1 },
  { 0, -1 },
  { 1, 1 },
  { 1, -1 },
  { -1, 1 },
  { -1, -1 },
}

--- Try to clear a failing retreat by opening one door toward the retreat
--- point. The same bounded, verified rescue Movement runs, in miniature:
--- only when the walk is already failing, only a door that reads closed,
--- unlocked and unbarricaded, re-read after the toggle, at most
--- Combat.RETREAT_DOORS attempts -- and nothing at all with allow_doors=false
--- or a door whose lock cannot be read.
local function retreatDoorRescue(ctx, spec, position)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  if spec.allow_doors ~= true then
    return false
  end
  if (spec.door_attempts or 0) >= Combat.RETREAT_DOORS then
    return false
  end
  local baseX, baseY = math.floor(position.x), math.floor(position.y)
  local dirX, dirY = spec.goal.x - position.x, spec.goal.y - position.y
  for index = 1, #DOOR_SCAN do
    local dx, dy = DOOR_SCAN[index][1], DOOR_SCAN[index][2]
    if (dx == 0 and dy == 0) or (dx * dirX + dy * dirY) > 0 then
      local square = Toolkit.gridSquare(baseX + dx, baseY + dy, position.z)
      if square ~= nil then
        local ok, objects = Toolkit.call(square, "getObjects")
        if ok and objects ~= nil then
          local size = Toolkit.listSize(objects) or 0
          local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS)
          for objectIndex = 0, scanned - 1 do
            local object = Toolkit.listGet(objects, objectIndex)
            if object ~= nil and isDoorObject(object) then
              local open = Toolkit.readBooleanOf(object, { "IsOpen", "isOpen" })
              if open == false then
                local locked = Toolkit.readBooleanOf(object, { "isLocked", "isLockedByKey" })
                local barricaded = Toolkit.readBooleanOf(object, { "isBarricaded" })
                local named = string.format("(%d, %d, %d)", baseX + dx, baseY + dy, position.z)
                if locked == true then
                  return nil, reasons.DOOR_LOCKED, string.format("the door at %s is locked", named)
                end
                if barricaded == true then
                  return nil, reasons.DOOR_BARRICADED, string.format("the door at %s is barricaded", named)
                end
                if locked ~= false or barricaded ~= false then
                  -- A reader is absent, and nil is not false: the door cannot
                  -- be proven safe to open, so it is left alone.
                  return false
                end
                spec.door_attempts = (spec.door_attempts or 0) + 1
                Toolkit.call(object, "ToggleDoor", ctx.player)
                if Toolkit.readBooleanOf(object, { "IsOpen", "isOpen" }) ~= true then
                  return false
                end
                spec.doors_opened = (spec.doors_opened or 0) + 1
                local queued = enqueueRetreatWalk(ctx, spec)
                if queued == nil then
                  return false
                end
                local marks = Toolkit.state(ctx).progress_marks
                if type(marks) == "table" then
                  -- The door that just opened gives the pathfinder a route it
                  -- did not have; holding the old stall mark would call the
                  -- very next poll stalled again. Bounded by the attempt
                  -- budget above.
                  marks.retreat_distance = nil
                end
                return "reenqueued"
              end
            end
          end
        end
      end
    end
  end
  return false
end

local function retreatValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(RETREAT_REQUIRES)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, RETREAT_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local distance, distanceCode, distanceDetail = Toolkit.readCount(args, "distance", {
    default = Combat.RETREAT_DEFAULT,
    minimum = Combat.RETREAT_MIN,
    maximum = Combat.RETREAT_MAX,
  })
  if distance == nil then
    return nil, distanceCode, distanceDetail
  end
  local allowDoors, doorsCode, doorsDetail = Toolkit.readFlag(args, "allow_doors", true)
  if allowDoors == nil then
    return nil, doorsCode, doorsDetail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  local snapshot = Toolkit.snapshot(ctx)
  local position = snapshot.position
  if type(position) ~= "table" then
    return nil, reasons.PRECONDITION_FAILED, "the character reports no position"
  end
  -- On failure the second return is the reason code and the third the detail.
  local nearest, nearestDistance, nearestDetail = nearestZombie(position)
  if nearest == nil then
    return nil, nearestDistance, nearestDetail
  end
  if nearest == false then
    return nil, reasons.PRECONDITION_FAILED, "no zombie is observed to retreat from"
  end
  local ux, uy = awayVector(position, nearest.point)
  -- The farthest loaded square along the away vector, stepped down one square
  -- at a time so an unloaded far edge shortens the retreat instead of
  -- refusing it. Bounded by the argument's own ceiling.
  local goal = nil
  for n = distance, Combat.RETREAT_MIN, -1 do
    local x = math.floor(position.x + (ux * n))
    local y = math.floor(position.y + (uy * n))
    local square, missing = Toolkit.gridSquare(x, y, position.z)
    if square ~= nil then
      goal = { x = x, y = y, z = position.z }
      break
    end
    if missing ~= nil and missing ~= "IsoCell.getGridSquare" then
      return Toolkit.unavailable(missing)
    end
  end
  if goal == nil then
    return nil, reasons.TARGET_NOT_LOADED, "no square in the retreat direction is loaded"
  end
  Toolkit.state(ctx).retreat = {
    goal = goal,
    distance = distance,
    allow_doors = allowDoors,
    nearest_before = nearestDistance,
    door_attempts = 0,
    doors_opened = 0,
  }
  return true
end

local function retreatBegin(_, _, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).retreat
  if spec == nil then
    return nil, Toolkit.reasons().INTERNAL_ERROR, "the command recorded no retreat point"
  end
  local queued, queueCode, queueDetail = enqueueRetreatWalk(ctx, spec)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  return true
end

local function retreatProgress(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).retreat
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no retreat point"
  end
  local snapshot = Toolkit.snapshot(ctx)
  local position = snapshot.position
  if type(position) ~= "table" then
    return nil, reasons.PRECONDITION_FAILED, "the character reports no position"
  end
  local gap = Toolkit.planeDistance(position, spec.goal.x, spec.goal.y)
  if position.z == spec.goal.z and gap <= Combat.RETREAT_ARRIVE then
    return "done"
  end
  local nearest = nearestZombie(position)
  if nearest == false then
    -- Nothing is observed to retreat from any more; the postcondition holds
    -- early and verify will read it again for the record.
    return "done"
  end
  local movement, elapsed = Toolkit.trackProgress(ctx, "retreat_distance", gap, { epsilon = 0.1 })
  if movement == "stalled" then
    local rescued, doorCode, doorDetail = retreatDoorRescue(ctx, spec, position)
    if rescued == "reenqueued" then
      return "running"
    end
    if doorCode ~= nil then
      return nil, doorCode, doorDetail
    end
    return nil,
      reasons.PATH_STUCK,
      string.format("the retreat has not gained ground for %d ms, still %.2f squares from its point", elapsed, gap)
  end
  local queue, queueCode, queueDetail = Toolkit.queueProgress(ctx)
  if queue == nil then
    return nil, queueCode, queueDetail
  end
  if queue == "done" then
    -- The walk drained short of the point: the other face of a blocked route.
    local rescued, doorCode, doorDetail = retreatDoorRescue(ctx, spec, position)
    if rescued == "reenqueued" then
      return "running"
    end
    if doorCode ~= nil then
      return nil, doorCode, doorDetail
    end
    return nil, reasons.PATH_NOT_FOUND, string.format("the retreat walk ended %.2f squares short", gap)
  end
  return "running"
end

local function retreatVerify(_, _, _, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).retreat
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no retreat point"
  end
  local snapshot = Toolkit.snapshot(ctx)
  local position = snapshot.position
  if type(position) ~= "table" then
    return nil, reasons.POSTCONDITION_FAILED, "the character reports no position to verify against"
  end
  local evidence = {
    kind = "retreated",
    nearest_before = spec.nearest_before,
    doors_opened = spec.doors_opened or 0,
  }
  -- On failure the second return is the reason code and the third the detail:
  -- a retreat whose world went unreadable is not one that succeeded.
  local nearest, nearestDistance, nearestDetail = nearestZombie(position)
  if nearest == nil then
    return nil, nearestDistance, nearestDetail
  end
  if nearest == false then
    evidence.no_zombie_observed = true
    return evidence
  end
  if nearestDistance > spec.nearest_before + Combat.RETREAT_EPSILON then
    evidence.nearest_after = nearestDistance
    return evidence
  end
  return nil,
    reasons.POSTCONDITION_FAILED,
    string.format(
      "the nearest zombie is still %.2f squares away (was %.2f)",
      nearestDistance,
      spec.nearest_before
    )
end

local Retreat = toolkit().declare({
  name = "combat.retreat",
  capability = toolkit().CAPABILITY.COMBAT_ASSIST,
  experimental = true,
  requires = RETREAT_REQUIRES,
  timeout_ms = Combat.RETREAT_TIMEOUT_MS,
  poll_interval_ms = Combat.POLL_MS,
  args = {
    distance = {
      type = ARG.NUMBER,
      integer = true,
      min = Combat.RETREAT_MIN,
      max = Combat.RETREAT_MAX,
      default = Combat.RETREAT_DEFAULT,
    },
    allow_doors = { type = ARG.BOOLEAN, default = true },
  },
  validate = retreatValidate,
  begin = retreatBegin,
  progress = retreatProgress,
  verify = retreatVerify,
})

Combat.Retreat = Retreat
toolkit().register(Retreat)

return Combat
