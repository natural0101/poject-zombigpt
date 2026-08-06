--[[
PZAgent.Adapters.Equipment -- equipment.equip and equipment.unequip.

Invariant: the equipped state is changed by the game's own timed actions --
ISEquipWeaponAction for a hand, ISWearClothing for a garment or a bag,
ISUnequipAction for taking either off -- and the postcondition is read back off
the character: the hand now holds that runtime id, or the worn set now does, or
no longer does. `setPrimaryHandItem` and its relatives are never called here.
They would make every ack succeed while leaving the animation, the weight and
the two-handed bookkeeping out of step with what the character is holding.

Which class to use is decided from the item, not from the command: an item that
reports a body location is worn, anything else goes in a hand. That means the
required symbols differ per command, so `requires` names only what every branch
needs and each branch checks its own class through Toolkit.construct, which
answers CAPABILITY_UNAVAILABLE with the class name. A build with no
ISWearClothing can still draw a weapon.

Unequipping accepts three ways of naming the target because only one of them
always exists. An item reference resolves for anything in a container, but a
worn garment lives in no container at all and a held weapon may be in neither --
so `hand` and `slot` name those directly, and PZAgent.Refs is left alone rather
than being taught a reference kind the Python side does not mint.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Equipment = {}
PZAgent.Adapters.Equipment = Equipment

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Game ticks the equip actions take. Vanilla's own menus pass a time here; it
--- is passed on rather than invented per call so the character equips at the
--- speed the player is used to.
Equipment.EQUIP_TIME = 50
Equipment.UNEQUIP_TIME = 50

Equipment.TIMEOUT_MS = 15000
Equipment.POLL_MS = 200

--- Everything either command needs on every path.
local REQUIRES = { "ISTimedActionQueue.add" }

--- Checked per branch, because which one is needed depends on the item.
Equipment.WEAPON_SYMBOLS = { "ISEquipWeaponAction", "ISEquipWeaponAction.new" }
Equipment.CLOTHING_SYMBOLS = { "ISWearClothing", "ISWearClothing.new" }
Equipment.UNEQUIP_SYMBOLS = { "ISUnequipAction", "ISUnequipAction.new" }

Equipment.HANDS = { primary = true, secondary = true, both = true }

local EQUIP_ARGS = { "item_ref", "hand" }
local UNEQUIP_ARGS = { "item_ref", "hand", "slot" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  STRING = "string",
  REF = "ref",
  ENUM = "enum",
}

--- Is this item worn rather than held? Read off the item, never guessed from
--- its type name.
local function wornKind(item)
  local location = toolkit().readStringOf(item, { "getBodyLocation", "getCanBeEquipped" })
  return type(location) == "string" and #location > 0, location
end

Equipment.wornKind = wornKind

-- ---------------------------------------------------------------------------
-- equipment.equip
-- ---------------------------------------------------------------------------

local function equipSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, EQUIP_ARGS, { "item_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local hand, handCode, handDetail = Toolkit.readText(args, "hand", { default = "primary", maximum = 16 })
  if hand == nil then
    return nil, handCode, handDetail
  end
  if not Equipment.HANDS[hand] then
    return nil, reasons.INVALID_ARGUMENT, "hand must be primary, secondary or both"
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local identity = tonumber(parsed.runtime_id)
  if identity == nil then
    return nil, reasons.INVALID_REF, "the item reference carries no numeric identity"
  end
  return { item_ref = itemRef, identity = identity, hand = hand }
end

Equipment.equipSpec = equipSpec

local Equip = nil

local function equipValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Equip.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = equipSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local worn, location = wornKind(item)
  spec.worn = worn
  spec.body_location = location
  local branch = worn and Equipment.CLOTHING_SYMBOLS or Equipment.WEAPON_SYMBOLS
  local branchOk, branchCode, branchDetail = Toolkit.requireSymbols(branch)
  if branchOk == nil then
    return nil, branchCode, branchDetail
  end
  if worn and spec.hand ~= "primary" then
    -- `hand` defaults to primary, so this only fires when a caller asked for a
    -- hand explicitly and the item turned out to be a garment.
    return nil, reasons.INVALID_ARGUMENT, "a worn item goes in a body slot, not in a hand"
  end
  local snapshot = Toolkit.snapshot(ctx)
  if worn and snapshot.worn ~= nil and snapshot.worn[spec.identity] ~= nil then
    return nil, reasons.INVALID_ARGUMENT, "the item is already worn"
  end
  if not worn and snapshot.hands ~= nil then
    local occupant = spec.hand == "secondary" and snapshot.hands.secondary or snapshot.hands.primary
    if occupant == spec.identity then
      return nil, reasons.INVALID_ARGUMENT, "the item is already in that hand"
    end
  end
  Toolkit.state(ctx).equip = spec
  return true
end

--- Both equip actions take the item out of the character's own inventory.
local function equipPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = equipSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local outcome, moveCode, moveDetail = PZAgent.Adapters.Inventory.bringToMain(ctx, spec.item_ref)
  if outcome == nil then
    return nil, moveCode, moveDetail
  end
  return true
end

local function equipStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = equipSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local worn = wornKind(item)
  local action, actionCode, actionDetail
  if worn then
    action, actionCode, actionDetail = Toolkit.construct("ISWearClothing", ctx.player, item, Equipment.EQUIP_TIME)
  else
    local primary = spec.hand ~= "secondary"
    local twoHands = spec.hand == "both" or Toolkit.readBooleanOf(item, { "isTwoHandWeapon" }) == true
    action, actionCode, actionDetail = Toolkit.construct(
      "ISEquipWeaponAction",
      ctx.player,
      item,
      Equipment.EQUIP_TIME,
      primary,
      twoHands
    )
  end
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

--- Did the equipped state actually change, and in the direction asked for?
local function equipVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = equipSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  if type(after) ~= "table" then
    return nil, reasons.POSTCONDITION_FAILED, "no observation of the character was available"
  end
  local state = Toolkit.state(ctx).equip
  local worn = state ~= nil and state.worn == true
  if worn then
    if after.worn ~= nil and after.worn[spec.identity] ~= nil then
      return {
        kind = "item_worn",
        item_ref = spec.item_ref,
        body_location = after.worn[spec.identity],
        worn_before = type(before) == "table" and before.worn ~= nil and before.worn[spec.identity] ~= nil or false,
      }
    end
    return nil, reasons.POSTCONDITION_FAILED, "the item is not in the character's worn items"
  end
  local hands = after.hands or {}
  local wanted = spec.hand == "secondary" and "secondary" or "primary"
  if hands[wanted] == spec.identity then
    return {
      kind = "item_in_hand",
      item_ref = spec.item_ref,
      hand = wanted,
      primary_before = type(before) == "table" and before.hands ~= nil and before.hands.primary or nil,
      primary_after = hands.primary,
      secondary_after = hands.secondary,
    }
  end
  return nil,
    reasons.POSTCONDITION_FAILED,
    string.format("the %s hand does not hold the item that was equipped", wanted)
end

Equip = toolkit().declare({
  name = "equipment.equip",
  -- No probe declares an equip capability, so there is nothing honest to point
  -- at: `requires` plus the per-branch checks are what gate this action.
  capability = nil,
  requires = REQUIRES,
  timeout_ms = Equipment.TIMEOUT_MS,
  poll_interval_ms = Equipment.POLL_MS,
  args = {
    item_ref = { type = ARG.REF, required = true, kinds = { item = true } },
    -- The same closed set equipSpec checks against, so a hand this adapter has
    -- no branch for is refused before the item is ever resolved.
    hand = { type = ARG.ENUM, values = Equipment.HANDS },
  },
  validate = equipValidate,
  prepare = equipPrepare,
  begin = equipStart,
  verify = equipVerify,
})

Equipment.Equip = Equip
toolkit().register(Equip)

-- ---------------------------------------------------------------------------
-- equipment.unequip
-- ---------------------------------------------------------------------------

--- Work out what is being taken off, from whichever of the three namings the
--- caller used. Exactly one is required: two would let them disagree.
local function unequipSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, UNEQUIP_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local given = 0
  for _, key in ipairs(UNEQUIP_ARGS) do
    if args[key] ~= nil then
      given = given + 1
    end
  end
  if given == 0 then
    return nil, reasons.INVALID_ARGUMENT, "name what to take off: item_ref, hand or slot"
  end
  if given > 1 then
    return nil, reasons.INVALID_ARGUMENT, "name what to take off exactly once"
  end
  local snapshot = Toolkit.snapshot(ctx)
  if args.hand ~= nil then
    local hand, handCode, handDetail = Toolkit.readText(args, "hand", { maximum = 16 })
    if hand == nil then
      return nil, handCode, handDetail
    end
    if hand ~= "primary" and hand ~= "secondary" then
      return nil, reasons.INVALID_ARGUMENT, "hand must be primary or secondary"
    end
    local identity = (snapshot.hands or {})[hand]
    if identity == nil then
      return nil, reasons.PRECONDITION_FAILED, string.format("the %s hand is empty", hand)
    end
    return { identity = identity, hand = hand, source = "hand" }
  end
  if args.slot ~= nil then
    local slot, slotCode, slotDetail = Toolkit.readText(args, "slot", { maximum = 64 })
    if slot == nil then
      return nil, slotCode, slotDetail
    end
    for identity, location in pairs(snapshot.worn or {}) do
      if location == slot then
        return { identity = identity, slot = slot, source = "slot" }
      end
    end
    return nil, reasons.PRECONDITION_FAILED, string.format("nothing is worn in the %s slot", slot)
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local identity = tonumber(parsed.runtime_id)
  if identity == nil then
    return nil, reasons.INVALID_REF, "the item reference carries no numeric identity"
  end
  return { identity = identity, item_ref = itemRef, source = "item_ref" }
end

Equipment.unequipSpec = unequipSpec

--- The live item behind a spec: from the hand, from the worn list, or from the
--- container its reference names.
local function unequipItem(ctx, spec)
  local Toolkit = toolkit()
  if spec.source == "hand" then
    local accessor = spec.hand == "secondary" and "getSecondaryHandItem" or "getPrimaryHandItem"
    local ok, item = Toolkit.call(ctx.player, accessor)
    if not ok or item == nil then
      return Toolkit.unavailable("IsoPlayer." .. accessor)
    end
    return item
  end
  if spec.source == "slot" then
    local item, missing = Toolkit.findWorn(ctx.player, spec.slot, spec.identity)
    if item == nil then
      if missing ~= nil then
        return Toolkit.unavailable(missing)
      end
      return nil, Toolkit.reasons().PRECONDITION_FAILED, "the slot no longer holds that item"
    end
    return item
  end
  return Toolkit.resolveItem(ctx, spec.item_ref)
end

local Unequip = nil

local function unequipValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Unequip.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local branch, branchCode, branchDetail = Toolkit.requireSymbols(Equipment.UNEQUIP_SYMBOLS)
  if branch == nil then
    return nil, branchCode, branchDetail
  end
  local spec, code, detail = unequipSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local snapshot = Toolkit.snapshot(ctx)
  local held = (snapshot.hands or {}).primary == spec.identity
    or (snapshot.hands or {}).secondary == spec.identity
    or (snapshot.worn or {})[spec.identity] ~= nil
  if not held then
    return nil, reasons.PRECONDITION_FAILED, "the item is neither held nor worn, so there is nothing to take off"
  end
  local item, itemCode, itemDetail = unequipItem(ctx, spec)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  Toolkit.state(ctx).unequip = spec
  return true
end

local function unequipStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = unequipSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local item, itemCode, itemDetail = unequipItem(ctx, spec)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISUnequipAction", ctx.player, item, Equipment.UNEQUIP_TIME)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

local function unequipVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).unequip
  if spec == nil then
    local parsed, code, detail = unequipSpec(args, ctx)
    if parsed == nil then
      return nil, code, detail
    end
    spec = parsed
  end
  if type(after) ~= "table" then
    return nil, reasons.POSTCONDITION_FAILED, "no observation of the character was available"
  end
  local hands = after.hands or {}
  local stillHeld = hands.primary == spec.identity or hands.secondary == spec.identity
  local stillWorn = (after.worn or {})[spec.identity] ~= nil
  if stillHeld or stillWorn then
    return nil,
      reasons.POSTCONDITION_FAILED,
      stillHeld and "the item is still in the character's hand" or "the item is still worn"
  end
  return {
    kind = "item_unequipped",
    item_ref = spec.item_ref,
    runtime_id = spec.identity,
    hand = spec.hand,
    slot = spec.slot,
    primary_before = type(before) == "table" and before.hands ~= nil and before.hands.primary or nil,
    primary_after = hands.primary,
    secondary_after = hands.secondary,
  }
end

Unequip = toolkit().declare({
  name = "equipment.unequip",
  capability = nil,
  requires = REQUIRES,
  timeout_ms = Equipment.TIMEOUT_MS,
  poll_interval_ms = Equipment.POLL_MS,
  -- None of the three is `required`: the rule is "exactly one", which a
  -- per-argument declaration cannot say, so unequipSpec is what refuses both
  -- naming nothing and naming two things that could disagree.
  args = {
    item_ref = { type = ARG.REF, kinds = { item = true } },
    -- No `both` here, unlike equip: a hand is emptied one at a time, and
    -- unequipSpec resolves the identity out of the hand that was named.
    hand = { type = ARG.ENUM, values = { primary = true, secondary = true } },
    -- A body location as the engine spells it -- "Torso", "Jacket". The
    -- dispatcher's own token alphabet is what keeps it a slot name.
    slot = { type = ARG.STRING },
  },
  validate = unequipValidate,
  begin = unequipStart,
  verify = unequipVerify,
})

Equipment.Unequip = Unequip
toolkit().register(Unequip)

return Equipment
