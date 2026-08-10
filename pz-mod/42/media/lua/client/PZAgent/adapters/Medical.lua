--[[
PZAgent.Adapters.Medical -- medical.bandage.

Invariant: the dressing is applied by the game's own ISApplyBandage against a
real BodyPart object, and the postcondition is read back off that body part --
bleeding false, or bandaged true. No flag on the body damage model is ever
written here. Writing `bleeding = false` would make every ack succeed and every
character die of a wound the agent had recorded as treated.

Which part and which dressing are both decided deterministically, and both are
decided from the observation rather than from the command: the worst bleeding
part wins, ties break on the part's own name, and dressings come from a closed
ordered list with the dirty ones last. A caller may name either explicitly, and
naming a part that is not bleeding is refused rather than quietly redirected.

The constructor's argument order is vanilla's documented
`(doctor, patient, item, bodyPart)`. There is no Build 42.20 here to check that
against; a build that orders them differently produces a bandage that does not
land, and the postcondition below is what refuses to call that a success.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walked
-- adapters/ in an order that ran this file before Toolkit.lua. The statement
-- form is deliberate -- the paren form is banned as dynamic loading -- and the
-- test harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Medical = {}
PZAgent.Adapters.Medical = Medical

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Dressings this adapter will pick on its own, best first.
---
--- Closed and ordered on purpose. Dirty dressings are last because they carry
--- an infection risk the agent should only take when there is nothing else;
--- anything outside this list has to be named explicitly with `item_ref`, so a
--- mod-added dressing is a decision the caller makes rather than one made here
--- by a name match.
Medical.DRESSINGS = {
  "Base.Bandage",
  "Base.RippedSheets",
  "Base.DenimStrips",
  "Base.LeatherStrips",
  "Base.RippedSheetsDirty",
  "Base.DenimStripsDirty",
}

--- Body parts a caller may name, spelled the way BodyPartType spells them.
---
--- Closed for the same reason the dressings are, and for one more: `body_part`
--- is used as a key into the observed body, so a name that arrived from the
--- wire unchecked would be a lookup on data an attacker chose. A build that
--- grows a part treats it only once it is listed here, which is the direction
--- to be wrong in.
Medical.BODY_PARTS = {
  Head = true,
  Neck = true,
  Torso_Upper = true,
  Torso_Lower = true,
  Groin = true,
  UpperArm_L = true,
  UpperArm_R = true,
  ForeArm_L = true,
  ForeArm_R = true,
  Hand_L = true,
  Hand_R = true,
  UpperLeg_L = true,
  UpperLeg_R = true,
  LowerLeg_L = true,
  LowerLeg_R = true,
  Foot_L = true,
  Foot_R = true,
}

Medical.TIMEOUT_MS = 30000
Medical.POLL_MS = 250

local REQUIRES = { "ISApplyBandage", "ISApplyBandage.new", "ISTimedActionQueue.add" }

local BANDAGE_ARGS = { "body_part", "item_ref" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  REF = "ref",
  ENUM = "enum",
}

--- The worst bleeding body part in a snapshot, or nil.
---
--- Severity first, then the part's own name, so two equally hurt arms are always
--- treated in the same order and a retry treats the same one.
function Medical.worstBleeding(snapshot)
  if type(snapshot) ~= "table" or type(snapshot.body) ~= "table" then
    return nil
  end
  local best = nil
  local bestName = nil
  for name, part in pairs(snapshot.body) do
    if part.bleeding == true then
      local severity = part.severity or 0
      if
        best == nil
        or severity > (best.severity or 0)
        or (severity == (best.severity or 0) and name < bestName)
      then
        best = part
        bestName = name
      end
    end
  end
  if best == nil then
    return nil
  end
  return bestName, best
end

--- The live BodyPart object behind a snapshot entry.
---
--- Re-read rather than cached: the snapshot carries the index the part was at,
--- and the object itself must come from the engine at the moment it is used.
function Medical.bodyPartAt(player, index)
  local Toolkit = toolkit()
  local ok, body = Toolkit.call(player, "getBodyDamage")
  if not ok or body == nil then
    return nil, "IsoPlayer.getBodyDamage"
  end
  local okParts, parts = Toolkit.call(body, "getBodyParts")
  if not okParts or parts == nil then
    return nil, "BodyDamage.getBodyParts"
  end
  local part = Toolkit.listGet(parts, index)
  if part == nil then
    return nil, "BodyDamage.getBodyParts().get"
  end
  return part
end

--- The dressing to use: the one the caller named, or the best one carried.
local function chooseDressing(ctx, itemRef)
  local Toolkit = toolkit()
  local Inventory = PZAgent.Adapters.Inventory
  if itemRef ~= nil then
    local item, code, detail = Toolkit.resolveItem(ctx, itemRef)
    if item == nil then
      return nil, code, detail
    end
    return { ref = itemRef, item = item }
  end
  for index = 1, #Medical.DRESSINGS do
    local hits = Inventory.search(ctx, { full_type = Medical.DRESSINGS[index], limit = 1 })
    if #hits == 1 and hits[1].ref ~= nil then
      local item, code, detail = Toolkit.resolveItem(ctx, hits[1].ref)
      if item == nil then
        return nil, code, detail
      end
      return { ref = hits[1].ref, item = item, full_type = Medical.DRESSINGS[index] }
    end
  end
  return nil,
    Toolkit.reasons().PRECONDITION_FAILED,
    "the character carries none of the dressings this adapter will choose on its own"
end

Medical.chooseDressing = chooseDressing

local function bandageSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, BANDAGE_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local snapshot = Toolkit.snapshot(ctx)
  -- Emptiness via PZAgent.Compat, not the global `next`, which the 2026-08-08
  -- live run proved Kahlua does not provide; hasEntries also answers false for
  -- a non-table, preserving the type check this line used to spell out.
  if not PZAgent.Compat.hasEntries(snapshot.body) then
    return Toolkit.unavailable("BodyDamage.getBodyParts")
  end
  local partName, part
  if args.body_part ~= nil then
    local named, namedCode, namedDetail = Toolkit.readText(args, "body_part", { maximum = 64 })
    if named == nil then
      return nil, namedCode, namedDetail
    end
    part = snapshot.body[named]
    if part == nil then
      return nil, reasons.INVALID_ARGUMENT, string.format("this character has no body part called %q", named)
    end
    if part.bleeding ~= true then
      return nil, reasons.PRECONDITION_FAILED, string.format("%s is not bleeding", named)
    end
    partName = named
  else
    partName, part = Medical.worstBleeding(snapshot)
    if partName == nil then
      return nil, reasons.PRECONDITION_FAILED, "nothing on this character is bleeding"
    end
  end
  local itemRef
  if args.item_ref ~= nil then
    local given, givenCode, givenDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
    if given == nil then
      return nil, givenCode, givenDetail
    end
    itemRef = given
  end
  return { body_part = partName, part = part, item_ref = itemRef, bleeding_before = true }
end

Medical.bandageSpec = bandageSpec

local Bandage = nil

local function bandageValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Bandage.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = bandageSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local dressing, dressingCode, dressingDetail = chooseDressing(ctx, spec.item_ref)
  if dressing == nil then
    return nil, dressingCode, dressingDetail
  end
  spec.dressing = dressing
  Toolkit.state(ctx).bandage = spec
  return true
end

--- ISApplyBandage takes the dressing out of the character's own inventory, so a
--- roll of bandages at the bottom of a backpack is fetched first.
local function bandagePrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = bandageSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local dressing, dressingCode, dressingDetail = chooseDressing(ctx, spec.item_ref)
  if dressing == nil then
    return nil, dressingCode, dressingDetail
  end
  local outcome, moveCode, moveDetail = PZAgent.Adapters.Inventory.bringToMain(ctx, dressing.ref)
  if outcome == nil then
    return nil, moveCode, moveDetail
  end
  Toolkit.state(ctx).fetched = outcome == "moving"
  return true
end

local function bandageStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = bandageSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local dressing, dressingCode, dressingDetail = chooseDressing(ctx, spec.item_ref)
  if dressing == nil then
    return nil, dressingCode, dressingDetail
  end
  local part, missing = Medical.bodyPartAt(ctx.player, spec.part.index)
  if part == nil then
    return Toolkit.unavailable(missing)
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISApplyBandage", ctx.player, ctx.player, dressing.item, part)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  Toolkit.state(ctx).applied_ref = dressing.ref
  return true
end

local function bandageVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  -- The spec is re-derived from the *before* snapshot rather than from the
  -- world as it is now: once the wound stops bleeding, "the worst bleeding
  -- part" is a different part, and verify would go looking at the wrong one.
  local partName = Toolkit.state(ctx).bandage ~= nil and Toolkit.state(ctx).bandage.body_part or args.body_part
  if partName == nil then
    local chosen = Medical.worstBleeding(before)
    partName = chosen
  end
  if partName == nil then
    return nil, reasons.POSTCONDITION_FAILED, "no bleeding body part was recorded for this command"
  end
  local was = type(before) == "table" and type(before.body) == "table" and before.body[partName] or nil
  local now = type(after) == "table" and type(after.body) == "table" and after.body[partName] or nil
  if now == nil then
    return Toolkit.unavailable("BodyDamage.getBodyParts")
  end
  local observed = {
    kind = "wound_dressed",
    body_part = partName,
    bleeding_before = was ~= nil and was.bleeding or nil,
    bleeding_after = now.bleeding,
    bandaged_after = now.bandaged,
    item_ref = Toolkit.state(ctx).applied_ref,
  }
  if now.bleeding == false or now.bandaged == true then
    return observed
  end
  return nil,
    reasons.POSTCONDITION_FAILED,
    string.format("%s is still bleeding and still reports no dressing", partName)
end

Bandage = toolkit().declare({
  name = "medical.bandage",
  capability = toolkit().CAPABILITY.MEDICAL_BANDAGE,
  requires = REQUIRES,
  timeout_ms = Medical.TIMEOUT_MS,
  poll_interval_ms = Medical.POLL_MS,
  args = {
    -- Neither is required: with nothing named the adapter treats the worst
    -- bleeding part with the best dressing carried, which is the deterministic
    -- choice the file exists to make.
    body_part = { type = ARG.ENUM, values = Medical.BODY_PARTS },
    item_ref = { type = ARG.REF, kinds = { item = true } },
  },
  validate = bandageValidate,
  prepare = bandagePrepare,
  begin = bandageStart,
  verify = bandageVerify,
})

Medical.Bandage = Bandage
toolkit().register(Bandage)

return Medical
