-- PZAgent.Adapters.Medical: which wound gets dressed, and what proves it was.
--
-- Two groups carry the file. "a queue that emptied dressed nothing" performs no
-- action and asserts the command fails: ISApplyBandage is constructed here and
-- never runs itself, so the part still bleeds, and an adapter that read its own
-- ack would report a treated wound on a character who is still losing blood.
-- "a body part is a name from a closed set" is the other: `body_part` is used as
-- a key into the observed body, so the declaration is what stops a name the
-- caller invented from ever reaching that lookup.

local ROOT = arg[0]:match("^(.*)test_adapter_medical%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Inventory", "Medical" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains
local same = Harness.same

local Toolkit = PZ.Adapters.Toolkit
local Medical = PZ.Adapters.Medical
local Bandage = Medical.Bandage
local REASON = PZ.Protocol.REASON

--- A dressing applied to a part stops that part bleeding. Aimed at whichever
--- BodyPart object the adapter handed the constructor, so a bandage built
--- against the wrong part heals nothing the test then asserts about.
local function installBandage()
  return Support.installAction("ISApplyBandage", function(_, args)
    local part = args[4]
    part.state.bleeding = false
    part.state.bandaged = true
  end)
end

local function scene(options)
  options = options or {}
  local body = nil
  local parts = {}
  if not options.no_body then
    parts = {
      Support.bodyPart({ part = "Head" }),
      -- The deeper wound, so "worst bleeding" has something to be right about.
      Support.bodyPart({ part = "ForeArm_L", bleeding = not options.unhurt, health = 70 }),
      Support.bodyPart({ part = "Hand_R", bleeding = not options.unhurt, health = 90 }),
    }
    body = Support.bodyDamage(parts)
  end

  local rock = Support.item({ id = 9, full_type = "Base.Rock", name = "Rock" })
  local dressing = Support.item({ id = 7, full_type = "Base.Bandage", name = "Bandage" })
  local main
  if options.no_dressing then
    main = Support.container({ rock })
  elseif options.in_bag then
    local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { dressing } })
    main = Support.container({ rock, satchel })
  else
    main = Support.container({ rock, dressing })
  end

  local player = Support.player({ x = 100, y = 200, z = 0, inventory = main, body = body })
  Support.installAction("ISWalkToTimedAction")
  Support.installAction("ISInventoryTransferAction", function(_, args)
    Support.moveItem(args[2], args[3], args[4])
  end)
  local bandage = installBandage()
  local queue = Support.installQueue()
  return {
    player = player,
    ctx = Support.context({ player = player, safety = options.safety }),
    parts = parts,
    bandage = bandage,
    queue = queue,
    dressing = dressing,
    main = main,
  }
end

local DRESSING = Support.itemRef("player-main", 7)
local BAGGED_DRESSING = Support.itemRef("carried:99", 7)
local ROCK = Support.itemRef("player-main", 9)
local SQUARE = Support.squareRef(101, 200, 0)

Harness.group("bandage refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Bandage:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({ part = "Head" })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  local detail
  code, detail = refuse({ body_part = "Tail" })
  equal(code, REASON.INVALID_ARGUMENT, "a part this character has no such thing as is not a target")
  contains(detail, "Tail", "and the detail says which name was asked for")

  code, detail = refuse({ body_part = "Head" })
  equal(code, REASON.PRECONDITION_FAILED, "a part that is not bleeding is refused, never quietly redirected")
  contains(detail, "not bleeding", "with the reason it was refused")

  code = refuse({ item_ref = SQUARE })
  equal(code, REASON.INVALID_REF, "a square is not a dressing")

  code = refuse({ item_ref = Support.itemRef("player-main", 404) })
  equal(code, REASON.INVALID_REF, "and an item that is not carried is a stale reference")

  local unhurt = scene({ unhurt = true })
  code, detail = refuse({}, unhurt.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "a character with no bleeding wound has nothing to dress")
  contains(detail, "bleeding", "and the detail says so")

  local bare = scene({ no_dressing = true })
  code, detail = refuse({}, bare.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "and a character carrying no dressing cannot be treated")
  contains(detail, "dressings", "naming what was looked for")

end

Harness.group("an explicitly named item is the caller's decision, and the wound is the check")
do
  -- Naming an item skips the closed dressing list on purpose, so a mod-added
  -- dressing is usable. The cost is that a rock validates too: nothing here can
  -- tell one from the other, and the postcondition below is the whole defence.
  local s = scene()
  local args = { item_ref = ROCK }
  ok(Bandage:validate(args, s.ctx), "an item the caller named is taken at their word")
  ok(Bandage:prepare(args, s.ctx), "and prepared")
  local before = Toolkit.observe(s.player)
  ok(Bandage:begin(args, s.ctx), "and applied")

  Support.drainQueue(s.queue)
  local evidence, code = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a rock held against a wound stops no bleeding")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails on the wound rather than on the item")
end

Harness.group("a build with no bandage action costs a capability, not a crash")
do
  local s = scene()
  Support.removeAction("ISApplyBandage")
  local accepted, code, detail = Bandage:validate({}, s.ctx)
  isNil(accepted, "with no bandage action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISApplyBandage", "naming the symbol this build did not have")

  local queueless = scene()
  Support.removeQueue()
  local _, queueCode, queueDetail = Bandage:validate({}, queueless.ctx)
  equal(queueCode, REASON.CAPABILITY_UNAVAILABLE, "and a build with no action queue is the same kind of gap")
  contains(queueDetail, "ISTimedActionQueue.add", "naming the symbol it could not reach")

  local blind = scene({ no_body = true })
  local _, bodyCode, bodyDetail = Bandage:validate({}, blind.ctx)
  equal(bodyCode, REASON.CAPABILITY_UNAVAILABLE, "a build that reports no body parts cannot be treated blind")
  contains(bodyDetail, "BodyDamage.getBodyParts", "naming the accessor that was missing")
end

Harness.group("bandage picks the worst wound and the best dressing")
do
  local s = scene()
  local name, part = Medical.worstBleeding(Toolkit.observe(s.player))
  equal(name, "ForeArm_L", "the deeper of two bleeding parts is the one treated")
  ok(part.severity > 0.2, "and it is the one the reading says is worse")

  ok(Bandage:validate({}, s.ctx), "a command naming nothing at all validates")
  equal(Toolkit.state(s.ctx).bandage.body_part, "ForeArm_L", "against the part chosen from the observation")
  equal(Toolkit.state(s.ctx).bandage.dressing.full_type, "Base.Bandage", "with the best dressing carried")
end

Harness.group("bandage fetches a dressing out of a bag before it applies it")
do
  local s = scene({ in_bag = true })
  local args = { item_ref = BAGGED_DRESSING }
  ok(Bandage:validate(args, s.ctx), "a dressing in a satchel is a dressing")
  ok(Bandage:prepare(args, s.ctx), "prepare succeeds")
  equal(#s.queue.added, 1, "one action was queued to fetch it")
  equal(s.queue.added[1].Type, "ISInventoryTransferAction", "and it is the same tagged transfer any move uses")

  local carried = scene()
  ok(Bandage:validate({ item_ref = DRESSING }, carried.ctx), "a dressing already in the main inventory validates")
  ok(Bandage:prepare({ item_ref = DRESSING }, carried.ctx), "and prepares")
  equal(#carried.queue.added, 0, "with nothing queued, because nothing needed fetching")
end

Harness.group("a queue that emptied dressed nothing")
do
  local s = scene()
  local args = {}
  ok(Bandage:validate(args, s.ctx), "the command validates")
  ok(Bandage:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Bandage:begin(args, s.ctx), "and starts")
  equal(#s.bandage.actions, 1, "one bandage action was constructed")

  -- The queue drains, as it would when the game finishes an action -- and the
  -- action itself never ran, which is exactly the case that must not become a
  -- success.
  Support.drainQueue(s.queue)
  local evidence, code, detail = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "an empty queue over a wound that still bleeds proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "ForeArm_L", "and the detail names the wound that was not treated")
end

Harness.group("a dressing the wound already carried is not one this command applied")
do
  local s = scene()
  -- A soaked bandage over a wound that is still losing blood. The part reads
  -- `bandaged` before the command starts, so "bandaged is true afterwards" is a
  -- fact that holds whether ISApplyBandage landed or never ran at all -- and an
  -- adapter that accepts it has verified something the action did not do.
  s.parts[2].state.bandaged = true
  local args = { body_part = "ForeArm_L" }
  ok(Bandage:validate(args, s.ctx), "a bleeding part may be dressed again")
  ok(Bandage:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Bandage:begin(args, s.ctx), "and starts")

  Support.drainQueue(s.queue)
  local evidence, code, detail = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "the dressing the part already carried proves nothing about this command")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "already", "and the detail says the dressing predates the command")

  -- The wound stopping is still the postcondition, dressing or no dressing.
  s.bandage.actions[1]:perform()
  local proof = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "a part that has stopped bleeding is the evidence")
  equal(proof.bleeding_after, false, "carrying the reading that settled it")
end

Harness.group("a dressing that was not there before is what proves one landed")
do
  local s = scene()
  local args = { body_part = "ForeArm_L" }
  ok(Bandage:validate(args, s.ctx), "the command validates")
  ok(Bandage:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Bandage:begin(args, s.ctx), "and starts")

  -- A dressing that landed on a wound that keeps bleeding: the part was not
  -- bandaged before and is now, which is this command's own effect.
  s.parts[2].state.bandaged = true
  local proof = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "a dressing that was not there before is an observation of this command")
  equal(proof.bandaged_after, true, "carrying the dressing the part now reports")
  equal(proof.bleeding_after, true, "and stating plainly that the wound is still bleeding")
end

Harness.group("the wound is read back off the body part it was aimed at")
do
  local s = scene()
  local args = {}
  ok(Bandage:validate(args, s.ctx), "the command validates")
  ok(Bandage:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Bandage:begin(args, s.ctx), "and starts")

  s.bandage.actions[1]:perform()
  local proof = Bandage:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "a part that has stopped bleeding is the evidence")
  equal(proof.kind, "wound_dressed", "named for what was observed")
  equal(proof.body_part, "ForeArm_L", "on the part that was treated")
  equal(proof.bleeding_before, true, "carrying the reading before")
  equal(proof.bleeding_after, false, "and the reading after")
  equal(proof.bandaged_after, true, "with the dressing the part now reports")
  equal(proof.item_ref, DRESSING, "and the dressing that was spent")

  local other = Toolkit.observe(s.player).body["Hand_R"]
  equal(other.bleeding, true, "the wound nobody asked about is untouched")
end

Harness.group("bandage stops for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local prepared, prepareCode = Bandage:prepare({}, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Bandage:begin({}, threatened.ctx)
  isNil(started, "and nothing starts into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
  equal(#threatened.queue.added, 0, "and nothing queued")
end

Harness.group("the adapter is registered where the dispatcher looks for it")
do
  equal(PZ.Adapters.BY_NAME["medical.bandage"], Bandage, "the bandage is registered under its action name")
  equal(Bandage.action, "medical.bandage", "and names itself under the key the dispatcher reads")
  ok(PZ.Protocol.isKnownAction(Bandage.action), "which is in the protocol whitelist")
  ok(type(Bandage.start) == "function", "the runtime's start() exists")
  ok(type(Bandage.poll) == "function", "and so does its poll()")
end

Harness.group("a body part is a name from a closed set")
do
  -- `body_part` is a key into the observed body. Declared as a free string it
  -- would be a lookup on whatever the caller sent; declared as an enum, a name
  -- the game does not have is refused before the adapter is reached.
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local dressed = checkArgs(Bandage, { body_part = "ForeArm_L", item_ref = DRESSING }, Support.SESSION)
  equal(dressed.body_part, "ForeArm_L", "a real part name survives the rebuild")
  equal(dressed.item_ref, DRESSING, "and so does the dressing that was named")

  local _, inventedCode = checkArgs(Bandage, { body_part = "Torso" }, Support.SESSION)
  equal(inventedCode, REASON.INVALID_ARGUMENT, "a plausible name the game does not use is still refused")

  local _, hostileCode = checkArgs(Bandage, { body_part = "getBodyDamage" }, Support.SESSION)
  equal(hostileCode, REASON.INVALID_ARGUMENT, "and a name chosen to look like an accessor never reaches a lookup")

  local _, unknownCode = checkArgs(Bandage, { part = "Head" }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, kindCode = checkArgs(Bandage, { item_ref = SQUARE }, Support.SESSION)
  equal(kindCode, REASON.INVALID_REF, "a square is not something to dress a wound with")

  local nothing = checkArgs(Bandage, {}, Support.SESSION)
  same(nothing, {}, "both arguments are optional, because the adapter chooses when neither is given")

  ok(Medical.BODY_PARTS["Torso_Upper"], "the declared set is the game's own spelling")
  isNil(Medical.BODY_PARTS["Torso"], "and holds nothing the observation would not key on")
end

Harness.finish("adapter_medical")
