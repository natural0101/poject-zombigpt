-- PZAgent.Adapters.Equipment: putting things on and taking them off.
--
-- The class to use is decided from the item rather than from the command, so
-- most of what follows is about that branch: a garment and a weapon take
-- different engine classes, fail with different missing symbols, and are proved
-- against different halves of the observation -- the worn set for one, the hand
-- for the other. The last two groups are the ones that matter: an equip whose
-- timed action was queued and never ran leaves the character holding exactly
-- what they held before, and that must not be a success.

local ROOT = arg[0]:match("^(.*)test_adapter_equipment%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Inventory", "Equipment" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Equipment = PZ.Adapters.Equipment
local Equip = Equipment.Equip
local Unequip = Equipment.Unequip
local REASON = PZ.Protocol.REASON

local function scene(options)
  options = options or {}
  local axe = Support.item({ id = 11, full_type = "Base.Axe", name = "Axe", two_handed = true })
  local knife = Support.item({ id = 12, full_type = "Base.HuntingKnife", name = "Hunting Knife" })
  -- A worn garment stays in the character's own inventory in this game, which
  -- is why an item reference can name one at all and why "the item is already
  -- worn" is a branch equip can reach.
  local jacket = Support.item({ id = 21, full_type = "Base.Jacket", name = "Jacket", body_location = "Jacket" })
  local main = Support.container({ axe, knife, jacket })
  local worn = {}
  if options.wearing_jacket then
    worn[#worn + 1] = Support.worn("Jacket", jacket)
  end
  local player = Support.player({
    x = 100,
    y = 200,
    z = 0,
    inventory = main,
    worn = worn,
    primary = options.primary and knife or nil,
  })

  Support.installAction("ISWalkToTimedAction")
  Support.installAction("ISInventoryTransferAction", function(_, args)
    Support.moveItem(args[2], args[3], args[4])
  end)
  local equipWeapon = Support.installAction("ISEquipWeaponAction", function(_, args)
    local holder, item, _, primary = args[1], args[2], args[3], args[4]
    if primary then
      holder.state.primary = item
    else
      holder.state.secondary = item
    end
  end)
  local wear = Support.installAction("ISWearClothing", function(_, args)
    local item = args[2]
    worn[#worn + 1] = Support.worn(item.fields.body_location, item)
  end)
  local takeOff = Support.installAction("ISUnequipAction", function(_, args)
    local holder, item = args[1], args[2]
    if holder.state.primary == item then
      holder.state.primary = nil
    end
    if holder.state.secondary == item then
      holder.state.secondary = nil
    end
    for index = 1, #worn do
      if worn[index].getItem() == item then
        table.remove(worn, index)
        break
      end
    end
  end)
  local queue = Support.installQueue()
  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    queue = queue,
    equipWeapon = equipWeapon,
    wear = wear,
    takeOff = takeOff,
    axe = axe,
    knife = knife,
    jacket = jacket,
    main = main,
    worn = worn,
  }
end

local AXE = Support.itemRef("player-main", 11)
local KNIFE = Support.itemRef("player-main", 12)
local JACKET = Support.itemRef("player-main", 21)

Harness.group("equip refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Equip:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "equipping nothing in particular is not a command")

  code = refuse({ item_ref = AXE, side = "left" })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ item_ref = AXE, hand = "third" })
  equal(code, REASON.INVALID_ARGUMENT, "a character has no third hand")

  code = refuse({ item_ref = Support.itemRef("player-main", 404) })
  equal(code, REASON.INVALID_REF, "an item that is not there is a stale reference")

  local _, detail = refuse({ item_ref = JACKET, hand = "secondary" })
  contains(detail, "body slot", "a garment asked into a hand names the mistake")

  local dressed = scene({ wearing_jacket = true })
  local wornCode, wornDetail = refuse({ item_ref = JACKET }, dressed.ctx)
  equal(wornCode, REASON.INVALID_ARGUMENT, "putting on what is already on is not work")
  contains(wornDetail, "already worn", "and the detail says so")

  local armed = scene({ primary = true })
  local heldCode, heldDetail = refuse({ item_ref = KNIFE }, armed.ctx)
  equal(heldCode, REASON.INVALID_ARGUMENT, "and neither is drawing what is already drawn")
  contains(heldDetail, "already in that hand", "naming the hand that already holds it")
end

Harness.group("a missing equip class costs one capability, naming it")
do
  local s = scene()
  Support.removeAction("ISEquipWeaponAction")
  local accepted, code, detail = Equip:validate({ item_ref = AXE }, s.ctx)
  isNil(accepted, "with no weapon action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISEquipWeaponAction", "naming the symbol this build did not have")
  -- The branches are independent on purpose: a build with no weapon action can
  -- still put a coat on.
  ok(Equip:validate({ item_ref = JACKET }, s.ctx), "and a garment still validates without it")

  local clothing = scene()
  Support.removeAction("ISWearClothing")
  local _, wearCode, wearDetail = Equip:validate({ item_ref = JACKET }, clothing.ctx)
  equal(wearCode, REASON.CAPABILITY_UNAVAILABLE, "the other branch reports its own gap")
  contains(wearDetail, "ISWearClothing", "naming its own class")

  local queueless = scene()
  Support.removeQueue()
  local _, queueCode, queueDetail = Equip:validate({ item_ref = AXE }, queueless.ctx)
  equal(queueCode, REASON.CAPABILITY_UNAVAILABLE, "a build with no action queue equips nothing at all")
  contains(queueDetail, "ISTimedActionQueue.add", "naming the one symbol every branch stands on")
end

Harness.group("equip picks the class from the item, not from the command")
do
  local s = scene()
  local args = { item_ref = AXE, hand = "both" }
  ok(Equip:validate(args, s.ctx), "an axe validates")
  ok(Equip:prepare(args, s.ctx), "prepares")
  equal(#s.queue.added, 0, "with nothing queued, because it was already in the inventory")
  ok(Equip:begin(args, s.ctx), "and starts")
  equal(#s.equipWeapon.actions, 1, "one weapon action was constructed")
  equal(#s.wear.actions, 0, "and no clothing action")
  equal(s.equipWeapon.actions[1].args[5], true, "a two-handed axe is equipped as one")

  local dressing = scene()
  local coat = { item_ref = JACKET }
  ok(Equip:validate(coat, dressing.ctx), "a jacket validates")
  ok(Equip:begin(coat, dressing.ctx), "and starts")
  equal(#dressing.wear.actions, 1, "one clothing action was constructed")
  equal(#dressing.equipWeapon.actions, 0, "and no weapon action")

  local entries = PZ.Safety.describeQueue(dressing.player)
  equal(
    PZ.Ownership.describe(entries, dressing.ctx.session_id).ownership,
    PZ.Protocol.OWNERSHIP.MOD,
    "and what reached the queue is tagged as ours"
  )
end

Harness.group("equip stops for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local prepared, prepareCode = Equip:prepare({ item_ref = AXE }, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Equip:begin({ item_ref = AXE }, threatened.ctx)
  isNil(started, "and nothing starts into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
  equal(#threatened.queue.added, 0, "and nothing queued")
end

Harness.group("an equip that was queued and never ran is not a success")
do
  local s = scene()
  local args = { item_ref = AXE }
  ok(Equip:validate(args, s.ctx), "the command validates")
  ok(Equip:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)

  -- The queue empties, as it does when the game cancels an action, and the
  -- character's hands are exactly as they were.
  Support.drainQueue(s.queue)
  local evidence, code, detail = Equip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "an empty hand after an equip proves nothing was equipped")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "primary hand", "and the detail names the hand that stayed empty")

  s.equipWeapon.actions[1]:perform()
  local proof = Equip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the hand holding the item is the evidence")
  equal(proof.kind, "item_in_hand", "named for what was observed")
  equal(proof.hand, "primary", "carrying the hand it went into")
  equal(proof.primary_after, 11, "and the identity that hand now holds")
  isNil(proof.primary_before, "with the hand that was empty before")
end

Harness.group("a garment is proved off the worn set, not off the queue")
do
  local s = scene()
  local args = { item_ref = JACKET }
  ok(Equip:validate(args, s.ctx), "the command validates")
  ok(Equip:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)

  Support.drainQueue(s.queue)
  local evidence, code, detail = Equip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a jacket still in the bag was not put on")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed")
  contains(detail, "worn items", "and the detail names the observation that did not hold")

  s.wear.actions[1]:perform()
  local proof = Equip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the garment appearing in the worn set is the evidence")
  equal(proof.kind, "item_worn", "named for what was observed")
  equal(proof.body_location, "Jacket", "carrying the slot it went into")
  equal(proof.worn_before, false, "and the fact that it was not worn before")
end

Harness.group("a secondary hand is a different observation from the primary one")
do
  local s = scene()
  local args = { item_ref = KNIFE, hand = "secondary" }
  ok(Equip:validate(args, s.ctx), "the command validates")
  ok(Equip:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  s.equipWeapon.actions[1]:perform()
  local proof = Equip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the off hand holding it is evidence too")
  equal(proof.hand, "secondary", "named for the hand that was asked for")
  equal(proof.secondary_after, 12, "carrying what that hand now holds")
  isNil(proof.primary_after, "and leaving the other hand out of it")
end

Harness.group("unequip refuses a command it cannot act on")
do
  local s = scene({ primary = true })
  local function refuse(args, ctx)
    local accepted, code, detail = Unequip:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code, detail = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "taking off nothing in particular is not a command")
  contains(detail, "item_ref, hand or slot", "and the detail lists the three ways to name a target")

  code, detail = refuse({ item_ref = KNIFE, hand = "primary" })
  equal(code, REASON.INVALID_ARGUMENT, "naming it twice is refused, because the two could disagree")
  contains(detail, "exactly once", "and the detail says why")

  code = refuse({ hand = "both" })
  equal(code, REASON.INVALID_ARGUMENT, "hands are emptied one at a time")

  local empty = scene()
  code, detail = refuse({ hand = "primary" }, empty.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "an empty hand has nothing to take off")
  contains(detail, "empty", "and the detail says which")

  code, detail = refuse({ slot = "Hat" }, empty.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "and neither has an empty slot")
  contains(detail, "Hat", "naming the slot that was asked about")

  code = refuse({ item_ref = AXE }, empty.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "an item that is merely carried is not equipped")
end

Harness.group("a missing unequip class costs one capability, naming it")
do
  local s = scene({ primary = true })
  Support.removeAction("ISUnequipAction")
  local accepted, code, detail = Unequip:validate({ hand = "primary" }, s.ctx)
  isNil(accepted, "with no unequip action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISUnequipAction", "naming the symbol this build did not have")
end

Harness.group("unequip proves the hand is empty, not that the action was queued")
do
  local s = scene({ primary = true })
  local args = { hand = "primary" }
  ok(Unequip:validate(args, s.ctx), "a full hand can be emptied")
  ok(Unequip:begin(args, s.ctx), "and the command starts")
  equal(#s.takeOff.actions, 1, "one unequip action was constructed")
  local before = Toolkit.observe(s.player)

  Support.drainQueue(s.queue)
  local evidence, code, detail = Unequip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a hand that still holds the knife proves nothing was taken off")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "still in the character's hand", "and the detail says what was still true")

  s.takeOff.actions[1]:perform()
  local proof = Unequip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the hand having emptied is the evidence")
  equal(proof.kind, "item_unequipped", "named for what was observed")
  equal(proof.runtime_id, 12, "carrying the identity that left the hand")
  equal(proof.primary_before, 12, "the hand before")
  isNil(proof.primary_after, "and the hand after")
end

Harness.group("a garment is taken off by slot and proved off the worn set")
do
  local s = scene({ wearing_jacket = true })
  local args = { slot = "Jacket" }
  ok(Unequip:validate(args, s.ctx), "a filled slot can be emptied")
  ok(Unequip:begin(args, s.ctx), "and the command starts")
  local before = Toolkit.observe(s.player)

  local evidence, code, detail = Unequip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a jacket still on the character was not taken off")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed")
  contains(detail, "still worn", "with the observation that was still true")

  s.takeOff.actions[1]:perform()
  local proof = Unequip:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the garment leaving the worn set is the evidence")
  equal(proof.slot, "Jacket", "carrying the slot it came off")
  equal(proof.runtime_id, 21, "and the identity that was in it")
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["equipment.equip"], Equip, "equip is registered under its action name")
  equal(PZ.Adapters.BY_NAME["equipment.unequip"], Unequip, "and so is unequip")
  ok(PZ.Protocol.isKnownAction(Equip.name), "both names are in the protocol whitelist")
  ok(PZ.Protocol.isKnownAction(Unequip.name), "including unequip's")
  -- No probe declares an equip capability, and declaring one anyway would gate
  -- the action on a token the sidecar never sets.
  isNil(Equip.capability, "equip claims no capability, because no probe reports one")
  isNil(Unequip.capability, "and neither does unequip")
end

Harness.group("the declaration is what the dispatcher builds the args from")
do
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local equipped = checkArgs(Equip, { item_ref = AXE, hand = "both" }, Support.SESSION)
  equal(equipped.item_ref, AXE, "the item reference survives the rebuild")
  equal(equipped.hand, "both", "and so does the hand that was asked for")

  local _, handCode = checkArgs(Equip, { item_ref = AXE, hand = "third" }, Support.SESSION)
  equal(handCode, REASON.INVALID_ARGUMENT, "a hand outside the closed set never reaches the adapter")

  local _, unknownCode = checkArgs(Equip, { item_ref = AXE, side = "left" }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, missingCode = checkArgs(Equip, { hand = "primary" }, Support.SESSION)
  equal(missingCode, REASON.INVALID_ARGUMENT, "and equipping nothing in particular is not a command")

  local bySlot = checkArgs(Unequip, { slot = "Jacket" }, Support.SESSION)
  equal(bySlot.slot, "Jacket", "unequip's slot survives the rebuild")
  isNil(bySlot.item_ref, "and nothing it was not given is invented")

  local _, bothCode = checkArgs(Unequip, { hand = "both" }, Support.SESSION)
  equal(bothCode, REASON.INVALID_ARGUMENT, "unequip's hand set is narrower than equip's, and says so here too")

  -- None of the three is declared required, so "name exactly one" is left to
  -- unequipSpec; this is the check that it still gets the chance to run.
  local empty = checkArgs(Unequip, {}, Support.SESSION)
  ok(type(empty) == "table", "an empty command reaches the adapter, which refuses it")
  local refused, refusedCode = Unequip:validate(empty, scene().ctx)
  isNil(refused, "and it does refuse it")
  equal(refusedCode, REASON.INVALID_ARGUMENT, "for naming nothing to take off")
end

Harness.finish("adapter_equipment")
