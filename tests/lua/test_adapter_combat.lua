-- PZAgent.Adapters.Combat: the ASSISTED combat rung, one bounded window at a
-- time.
--
-- The cases that matter most here are the lies the adapters must refuse to
-- tell: an equip that was queued and never ran, a shove whose press returned
-- while the zombie kept walking, an engage window whose attack was pressed and
-- whose target still stands. Every success below is minted off a re-observed
-- target -- prone, dead, honestly absent, or measurably farther away -- and
-- every window is bounded by its swing budget and its clock, with interruption
-- consulted on every poll.

local ROOT = arg[0]:match("^(.*)test_adapter_combat%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Combat" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Combat = PZ.Adapters.Combat
local EquipBest = Combat.EquipBest
local Shove = Combat.Shove
local Engage = Combat.Engage
local Retreat = Combat.Retreat
local REASON = PZ.Protocol.REASON

--- A door for the retreat rescue, owned by this file the way the movement
--- tests own theirs: only the readers the fields imply exist, so deleting one
--- is how "this build cannot read that" is expressed.
local function door(fields)
  fields = fields or {}
  local state = { open = fields.open == true }
  local object = { state = state }
  object.getObjectName = function()
    return "Door"
  end
  object.IsOpen = function()
    return state.open
  end
  if not fields.no_lock_reader then
    object.isLocked = function()
      return fields.locked == true
    end
  end
  if not fields.no_barricade_reader then
    object.isBarricaded = function()
      return fields.barricaded == true
    end
  end
  object.ToggleDoor = function()
    if not fields.toggle_sticks then
      state.open = not state.open
    end
  end
  return object
end

--- One combat scene: an armed character with a small arsenal, a zombie in
--- reach, a cell, a queue and the press methods -- each removable through
--- `options` so a missing engine surface is one deleted field away.
local function scene(options)
  options = options or {}
  local axe = Support.item({
    id = 11, full_type = "Base.Axe", name = "Axe",
    category = "Weapon", condition = 8, condition_max = 10, weight = 3,
  })
  local bat = Support.item({
    id = 12, full_type = "Base.Bat", name = "Baseball Bat",
    category = "Weapon", condition = 9, condition_max = 10, weight = 2,
  })
  -- A weapon whose wear cannot be read, and one that reads broken: the
  -- ranking must exclude both rather than guess.
  local blindKnife = Support.item({ id = 13, name = "Knife", category = "Weapon", weight = 0.5 })
  local brokenPipe = Support.item({
    id = 14, name = "Pipe", category = "Weapon", condition = 0, condition_max = 10, weight = 1,
  })
  local apple = Support.item({ id = 15, name = "Apple", category = "Food", weight = 0.2 })
  local items = options.items or { axe, bat, blindKnife, brokenPipe, apple }
  local main = Support.container(items)
  local player = Support.player({
    x = options.x or 100,
    y = options.y or 200,
    z = 0,
    inventory = main,
    primary = options.primary,
  })
  local record = { faced = 0, shoves = 0, attacks = 0 }
  if options.no_face ~= true then
    player.faceThisObject = function()
      record.faced = record.faced + 1
      return true
    end
  end
  if options.no_shove ~= true then
    player.setForceShove = function()
      record.shoves = record.shoves + 1
      if options.on_shove then
        options.on_shove(record.shoves)
      end
      return true
    end
  end
  if options.no_attack ~= true then
    player.pressAttack = function()
      record.attacks = record.attacks + 1
      if options.on_attack then
        options.on_attack(record.attacks)
      end
      return true
    end
  end
  local zombies = options.zombies
  if zombies == nil then
    zombies = { Support.zombie({ id = 71, x = 101.5, y = 200, on_floor = false, crawling = false, dead = false }) }
  end
  if options.no_zombie_list then
    Support.installCell(options.squares or {})
  else
    Support.installCell(options.squares or {}, zombies)
  end
  local queue = Support.installQueue()
  local equipWeapon = Support.installAction("ISEquipWeaponAction", function(_, actionArgs)
    local holder, item, _, primary = actionArgs[1], actionArgs[2], actionArgs[3], actionArgs[4]
    if primary then
      holder.state.primary = item
    end
  end)
  local walk = Support.installAction("ISWalkToTimedAction")
  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    queue = queue,
    equipWeapon = equipWeapon,
    walk = walk,
    record = record,
    zombies = zombies,
    axe = axe,
    bat = bat,
    blindKnife = blindKnife,
    brokenPipe = brokenPipe,
    items = items,
    main = main,
  }
end

local TARGET = Support.zombieRef(71)

Harness.group("equip_best ranks by condition fraction, then weight, then id -- and excludes the unreadable")
do
  local s = scene()
  ok(EquipBest:validate({}, s.ctx), "with weapons carried the command validates")
  local spec = Toolkit.state(s.ctx).equip_best
  equal(spec.identity, 12, "the bat wins: 9/10 outranks 8/10")
  equal(spec.condition, 9, "carrying the condition it was ranked on")
  equal(spec.condition_max, 10, "and its maximum")
  equal(spec.considered, 4, "four carried items looked like weapons; the apple never did")

  -- Same fraction, different weight: the lighter one wins.
  local tied = scene({ items = {
    Support.item({ id = 21, name = "Axe", category = "Weapon", condition = 8, condition_max = 10, weight = 3 }),
    Support.item({ id = 22, name = "Hammer", category = "Weapon", condition = 8, condition_max = 10, weight = 1.5 }),
  } })
  ok(EquipBest:validate({}, tied.ctx), "the tie validates")
  equal(Toolkit.state(tied.ctx).equip_best.identity, 22, "equal wear picks the lighter weapon")

  -- Same fraction, same weight: the lower runtime id, so the pick never
  -- depends on engine list order.
  local twins = scene({ items = {
    Support.item({ id = 32, name = "Knife", category = "Weapon", condition = 5, condition_max = 10, weight = 1 }),
    Support.item({ id = 31, name = "Knife", category = "Weapon", condition = 5, condition_max = 10, weight = 1 }),
  } })
  ok(EquipBest:validate({}, twins.ctx), "the twin case validates")
  equal(Toolkit.state(twins.ctx).equip_best.identity, 31, "identical twins pick the lower id, deterministically")

  -- A weapon whose condition max cannot be read stays eligible but ranks
  -- below anything measurable.
  local half = scene({ items = {
    Support.item({ id = 41, name = "Bar", category = "Weapon", condition = 9, weight = 1 }),
    Support.item({ id = 42, name = "Plank", category = "Weapon", condition = 2, condition_max = 10, weight = 4 }),
  } })
  ok(EquipBest:validate({}, half.ctx), "the unreadable-max case validates")
  equal(Toolkit.state(half.ctx).equip_best.identity, 42, "a measurable 2/10 outranks an unnormalisable 9/?")

  local bare = scene({ items = {
    Support.item({ id = 13, name = "Knife", category = "Weapon", weight = 0.5 }),
    Support.item({ id = 14, name = "Pipe", category = "Weapon", condition = 0, condition_max = 10 }),
    Support.item({ id = 15, name = "Apple", category = "Food" }),
  } })
  local none, noneCode, noneDetail = EquipBest:validate({}, bare.ctx)
  isNil(none, "unreadable and broken weapons are not usable weapons")
  equal(noneCode, REASON.PRECONDITION_FAILED, "so there is nothing to equip")
  contains(noneDetail, "no usable weapon", "and the detail says exactly that")

  local junk = scene()
  local refused, junkCode = EquipBest:validate({ speed = "fast" }, junk.ctx)
  isNil(refused, "an unknown argument is refused")
  equal(junkCode, REASON.INVALID_ARGUMENT, "rather than ignored")

  local classless = scene()
  Support.removeAction("ISEquipWeaponAction")
  local missing, missingCode, missingDetail = EquipBest:validate({}, classless.ctx)
  isNil(missing, "with no equip class there is nothing to validate")
  equal(missingCode, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(missingDetail, "ISEquipWeaponAction", "naming the class this build did not have")
end

Harness.group("equip_best proves the hand, not the queue")
do
  local s = scene()
  ok(EquipBest:validate({}, s.ctx), "the command validates")
  local before = Toolkit.observe(s.player)
  ok(EquipBest:begin({}, s.ctx), "and starts")
  equal(#s.equipWeapon.actions, 1, "one equip action was constructed")
  equal(s.equipWeapon.actions[1].args[4], true, "into the primary hand")

  Support.drainQueue(s.queue)
  local evidence, code, detail = EquipBest:verify(before, Toolkit.observe(s.player), {}, s.ctx)
  isNil(evidence, "an empty hand after the equip proves nothing was equipped")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "primary hand", "naming the observation that did not hold")

  s.equipWeapon.actions[1]:perform()
  local proof = EquipBest:verify(before, Toolkit.observe(s.player), {}, s.ctx)
  ok(proof ~= nil, "the hand holding the bat is the evidence")
  equal(proof.kind, "best_weapon_equipped", "named for what was observed")
  equal(proof.runtime_id, 12, "carrying the identity that hand now holds")
  equal(proof.primary_after, 12, "as the hand reading")
  equal(proof.condition, 9, "with the condition the ranking used")
  isNil(proof.unchanged_is_success, "a hand that changed claims no unchanged success")

  local armed = scene()
  armed.player.state.primary = armed.bat
  ok(EquipBest:validate({}, armed.ctx), "an already-armed character still validates")
  equal(EquipBest:begin({}, armed.ctx), "done", "with nothing to queue")
  equal(#armed.equipWeapon.actions, 0, "no action was constructed")
  local unchanged = EquipBest:verify(Toolkit.observe(armed.player), Toolkit.observe(armed.player), {}, armed.ctx)
  ok(unchanged ~= nil, "the best weapon already in the hand is a success")
  equal(unchanged.unchanged_is_success, true, "declared as an unchanged one")
end

Harness.group("shove refuses what it cannot act on, naming the gap")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Shove:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "shoving nothing in particular is not a command")

  code = refuse({ target_ref = Support.itemRef("player-main", 11) })
  equal(code, REASON.INVALID_REF, "an item reference is not a zombie")

  local detail
  code, detail = refuse({ target_ref = Support.zombieRef(404) })
  equal(code, REASON.PRECONDITION_FAILED, "a zombie the scan cannot find is not a target")
  contains(detail, "no longer observed", "and the detail says so honestly")

  local far = scene({ zombies = { Support.zombie({ id = 71, x = 105, y = 200 }) } })
  code, detail = refuse({ target_ref = TARGET }, far.ctx)
  equal(code, REASON.TARGET_OUT_OF_RANGE, "a zombie past arm's reach needs a walk, not a lunge")
  contains(detail, "squares away", "with the measured distance")

  local upstairs = scene({ zombies = { Support.zombie({ id = 71, x = 100, y = 200, z = 1 }) } })
  code = refuse({ target_ref = TARGET }, upstairs.ctx)
  equal(code, REASON.TARGET_OUT_OF_RANGE, "and a zombie on another floor is out of range whatever the plane says")

  local pressless = scene({ no_shove = true })
  code, detail = refuse({ target_ref = TARGET }, pressless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "with no shove press the gap is a capability")
  contains(detail, "setForceShove", "naming the probed spellings")

  local listless = scene({ no_zombie_list = true })
  code, detail = refuse({ target_ref = TARGET }, listless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "an unreadable zombie list is a capability gap")
  contains(detail, "getZombieList", "naming the symbol, never 'no such zombie'")
end

Harness.group("a shove is proved off the re-observed target, by state or by distance")
do
  local felled = scene()
  local zombie = felled.zombies[1]
  local args = { target_ref = TARGET }
  ok(Shove:validate(args, felled.ctx), "a zombie in reach validates")
  ok(Shove:begin(args, felled.ctx), "and the shove starts")
  equal(felled.record.shoves, 1, "one press was made")
  equal(felled.record.faced, 1, "after facing the target")

  -- The press landed: the game puts the zombie on the floor.
  zombie.state.on_floor = true
  equal(Shove:progress(args, felled.ctx), "done", "a prone target ends the window")
  local proof = Shove:verify(nil, nil, args, felled.ctx)
  ok(proof ~= nil, "the prone re-observation is the evidence")
  equal(proof.kind, "target_shoved", "named for what was observed")
  equal(proof.target_state_after, "prone", "carrying the state that was read")
  equal(proof.state_before, "standing", "beside the state before")

  local pushed = scene()
  ok(Shove:validate(args, pushed.ctx), "the push-back case validates")
  ok(Shove:begin(args, pushed.ctx), "and starts")
  pushed.zombies[1].state.x = 104
  equal(Shove:progress(args, pushed.ctx), "done", "a target visibly farther away ends the window")
  local shoved = Shove:verify(nil, nil, args, pushed.ctx)
  ok(shoved ~= nil, "the grown distance is the evidence")
  equal(shoved.pushed_back, true, "named as a push-back")
  equal(shoved.distance_before, 1.5, "with the distance before")
  ok(shoved.distance_after > 3, "and the larger distance after")

  -- The anti-lie case: the press returned and the zombie kept coming.
  local swallowed = scene()
  ok(Shove:validate(args, swallowed.ctx), "the swallowed-press case validates")
  ok(Shove:begin(args, swallowed.ctx), "and starts")
  equal(Shove:progress(args, swallowed.ctx), "running", "an unchanged target keeps the window open")
  swallowed.ctx.now_ms = Support.NOW + Combat.SHOVE_WINDOW_MS
  equal(Shove:progress(args, swallowed.ctx), "done", "until the clock closes it")
  local evidence, code, detail = Shove:verify(nil, nil, args, swallowed.ctx)
  isNil(evidence, "a press that changed nothing observable is not a success")
  equal(code, REASON.POSTCONDITION_FAILED, "it failed its postcondition")
  contains(detail, "neither felled nor pushed back", "with the honest picture")

  local vanishing = scene()
  ok(Shove:validate(args, vanishing.ctx), "the vanishing case validates")
  ok(Shove:begin(args, vanishing.ctx), "and starts")
  vanishing.zombies[1].state.present = false
  equal(Shove:progress(args, vanishing.ctx), "done", "an absent target ends the window")
  local gone = Shove:verify(nil, nil, args, vanishing.ctx)
  ok(gone ~= nil, "the absence is evidence")
  equal(gone.target_gone, true, "named as exactly that, never as a state nobody read")
end

Harness.group("engage gates on the weapon it can observe and nothing it cannot")
do
  local args = { target_ref = TARGET }

  local unarmed = scene()
  local refused, code, detail = Engage:validate(args, unarmed.ctx)
  isNil(refused, "an empty hand does not engage")
  equal(code, REASON.PRECONDITION_FAILED, "as a precondition")
  contains(detail, "no weapon is equipped", "naming the empty hand")

  local blind = scene()
  blind.player.state.primary = blind.blindKnife
  refused, code, detail = Engage:validate(args, blind.ctx)
  isNil(refused, "an unreadable condition does not engage")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "as the capability gap it is, not as a broken weapon")
  contains(detail, "getCondition", "naming the reader this build did not have")

  local broken = scene()
  broken.player.state.primary = broken.brokenPipe
  refused, code, detail = Engage:validate(args, broken.ctx)
  isNil(refused, "a broken weapon does not engage")
  equal(code, REASON.PRECONDITION_FAILED, "as a precondition")
  contains(detail, "broken", "naming the weapon's state")

  local pressless = scene({ no_attack = true })
  pressless.player.state.primary = pressless.bat
  refused, code, detail = Engage:validate(args, pressless.ctx)
  isNil(refused, "with no attack press there is nothing to engage with")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "pressAttack", "naming the probed spellings")

  local s = scene()
  s.player.state.primary = s.bat
  refused, code = Engage:validate({ target_ref = TARGET, max_swings = 4 }, s.ctx)
  isNil(refused, "four swings is past the window's ceiling")
  equal(code, REASON.INVALID_ARGUMENT, "and refused as an argument")
  refused, code = Engage:validate({ target_ref = TARGET, max_swings = 0 }, s.ctx)
  isNil(refused, "and zero swings is no window at all")
  equal(code, REASON.INVALID_ARGUMENT, "refused the same way")

  local far = scene({ zombies = { Support.zombie({ id = 71, x = 110, y = 200 }) } })
  far.player.state.primary = far.bat
  refused, code = Engage:validate(args, far.ctx)
  isNil(refused, "a distant zombie is not engaged")
  equal(code, REASON.TARGET_OUT_OF_RANGE, "the mission walks first")
end

Harness.group("one engage window: bounded swings, bounded clock, success only on an observed down")
do
  local args = { target_ref = TARGET }

  -- The anti-lie case first: every swing pressed, the target still standing.
  local s = scene()
  s.player.state.primary = s.bat
  ok(Engage:validate(args, s.ctx), "the window validates")
  ok(Engage:begin(args, s.ctx), "and opens")
  equal(s.record.attacks, 1, "the first swing was pressed")
  equal(s.record.faced, 1, "after facing the target")
  equal(Engage:progress(args, s.ctx), "running", "a standing target keeps the window open")
  equal(s.record.attacks, 2, "and the second swing was pressed")
  equal(Engage:progress(args, s.ctx), "running", "the window stays open awaiting an outcome")
  equal(s.record.attacks, 2, "but the swing budget is spent: no third press, ever")
  s.ctx.now_ms = Support.NOW + Combat.ENGAGE_WINDOW_MS
  equal(Engage:progress(args, s.ctx), "done", "the clock closes the window")
  local evidence, code, detail = Engage:verify(nil, nil, args, s.ctx)
  isNil(evidence, "an attack that left the target standing is not a success")
  equal(code, REASON.POSTCONDITION_FAILED, "it failed its postcondition")
  contains(detail, "still reads standing", "with the honest picture for the mission to decide on")
  contains(detail, "2 swing", "and the swings that were spent")

  -- The observed down: the swing puts the target on the floor.
  local felled = scene()
  felled.player.state.primary = felled.bat
  ok(Engage:validate(args, felled.ctx), "the felling window validates")
  ok(Engage:begin(args, felled.ctx), "and opens")
  felled.zombies[1].state.on_floor = true
  equal(Engage:progress(args, felled.ctx), "done", "a prone target closes the window early")
  local proof = Engage:verify(nil, nil, args, felled.ctx)
  ok(proof ~= nil, "the prone re-observation is the evidence")
  equal(proof.kind, "engage_window", "named for the bounded thing it was")
  equal(proof.target_state_after, "prone", "carrying the observed state")
  equal(proof.swings_attempted, 1, "and exactly the swings that were pressed")

  local slain = scene()
  slain.player.state.primary = slain.bat
  ok(Engage:validate(args, slain.ctx), "the killing window validates")
  ok(Engage:begin(args, slain.ctx), "and opens")
  slain.zombies[1].state.dead = true
  equal(Engage:progress(args, slain.ctx), "done", "a dead target closes the window")
  equal(Engage:verify(nil, nil, args, slain.ctx).target_state_after, "dead", "and is named dead, as read")

  local vanishing = scene()
  vanishing.player.state.primary = vanishing.bat
  ok(Engage:validate(args, vanishing.ctx), "the vanishing window validates")
  ok(Engage:begin(args, vanishing.ctx), "and opens")
  vanishing.zombies[1].state.present = false
  equal(Engage:progress(args, vanishing.ctx), "done", "an absent target closes the window")
  equal(Engage:verify(nil, nil, args, vanishing.ctx).target_gone, true, "named absent, never guessed dead")

  -- One swing asked for is one swing pressed, whatever the clock allows.
  local single = scene()
  single.player.state.primary = single.bat
  ok(Engage:validate({ target_ref = TARGET, max_swings = 1 }, single.ctx), "a one-swing window validates")
  ok(Engage:begin({ target_ref = TARGET, max_swings = 1 }, single.ctx), "and opens")
  equal(Engage:progress({ target_ref = TARGET, max_swings = 1 }, single.ctx), "running", "the window waits")
  equal(single.record.attacks, 1, "without a second press")

  equal(Engage.timeout_ms, Combat.ENGAGE_WINDOW_MS, "the declared timeout IS the window bound")
end

Harness.group("interruption outranks the window on every poll")
do
  local args = { target_ref = TARGET }
  local s = scene()
  s.player.state.primary = s.bat
  ok(Engage:validate(args, s.ctx), "the window validates under a calm sky")
  ok(Engage:begin(args, s.ctx), "and opens")
  s.ctx.safety = Support.takenOver()
  local outcome, code = Engage:progress(args, s.ctx)
  isNil(outcome, "the player taking the controls stops the window mid-poll")
  equal(code, REASON.USER_TAKEOVER, "with the takeover reason")
  equal(s.record.attacks, 1, "and no further swing was pressed")

  local threatened = scene({ safety = Support.threatened() })
  threatened.player.state.primary = threatened.bat
  local refused, threatCode = Shove:begin(args, threatened.ctx)
  isNil(refused, "nothing starts into a reflex-guard rung that blocks work")
  equal(threatCode, REASON.THREAT_INTERRUPTED, "with the guard's reason")
  equal(threatened.record.shoves, 0, "and no press was made")
end

Harness.group("retreat picks the away vector deterministically and walks it")
do
  local squares = {}
  for x = 90, 110 do
    squares[Support.squareKey(x, 200, 0)] = Support.square(x, 200, 0, {})
  end
  local s = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, s.ctx), "with a zombie observed the retreat validates")
  local spec = Toolkit.state(s.ctx).retreat
  equal(spec.goal.x, 92, "the goal square lies the default eight squares directly away from the threat")
  equal(spec.goal.y, 200, "on the same row")
  equal(spec.nearest_before, 3, "recording the gap it starts from")
  ok(Retreat:begin({}, s.ctx), "the walk starts")
  equal(#s.walk.actions, 1, "one walk action was constructed")
  equal(s.walk.actions[1].args[2], squares[Support.squareKey(92, 200, 0)], "aimed at exactly that square")

  local overlapped = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 100, y = 200 }) } })
  ok(Retreat:validate({}, overlapped.ctx), "a zombie on the character's own point still validates")
  equal(Toolkit.state(overlapped.ctx).retreat.goal.x, 108, "with the documented deterministic pick: east")

  local edge = { [Support.squareKey(95, 200, 0)] = Support.square(95, 200, 0, {}) }
  local shortened = scene({ squares = edge, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, shortened.ctx), "an unloaded far edge shortens the retreat instead of refusing it")
  equal(Toolkit.state(shortened.ctx).retreat.goal.x, 95, "to the farthest loaded square on the vector")

  local nothing = scene({ squares = squares, zombies = {} })
  local refused, code, detail = Retreat:validate({}, nothing.ctx)
  isNil(refused, "with no zombie observed there is nothing to retreat from")
  equal(code, REASON.PRECONDITION_FAILED, "as a precondition")
  contains(detail, "no zombie", "and the detail says so")

  local bounds = scene({ squares = squares })
  local tooFar, boundCode = Retreat:validate({ distance = 30 }, bounds.ctx)
  isNil(tooFar, "a thirty-square flight is past the ceiling")
  equal(boundCode, REASON.INVALID_ARGUMENT, "and refused as an argument")
end

Harness.group("retreat succeeds only when the gap to the nearest zombie grew")
do
  local squares = {}
  for x = 90, 110 do
    squares[Support.squareKey(x, 200, 0)] = Support.square(x, 200, 0, {})
  end
  local s = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, s.ctx), "the retreat validates")
  ok(Retreat:begin({}, s.ctx), "and starts")
  equal(Retreat:progress({}, s.ctx), "running", "mid-walk it is running")

  -- The walk lands: the character stands on the retreat square.
  s.player.state.x = 92.2
  s.player.state.y = 200.3
  Support.drainQueue(s.queue)
  equal(Retreat:progress({}, s.ctx), "done", "arrival ends the walk")
  local proof = Retreat:verify(nil, nil, {}, s.ctx)
  ok(proof ~= nil, "the grown gap is the evidence")
  equal(proof.kind, "retreated", "named for what happened")
  equal(proof.nearest_before, 3, "the gap before")
  ok(proof.nearest_after > 10, "and the much larger gap after")

  -- The zombie kept pace: arriving proves nothing, and the command says so.
  local chased = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, chased.ctx), "the chased retreat validates")
  ok(Retreat:begin({}, chased.ctx), "and starts")
  chased.player.state.x = 92.2
  chased.zombies[1].state.x = 93
  Support.drainQueue(chased.queue)
  equal(Retreat:progress({}, chased.ctx), "done", "arrival still ends the walk")
  local evidence, code, detail = Retreat:verify(nil, nil, {}, chased.ctx)
  isNil(evidence, "a zombie that kept pace means no ground was gained")
  equal(code, REASON.POSTCONDITION_FAILED, "so the retreat failed honestly")
  contains(detail, "still", "with the measured picture")

  local cleared = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, cleared.ctx), "the clearing retreat validates")
  ok(Retreat:begin({}, cleared.ctx), "and starts")
  cleared.zombies[1].state.present = false
  equal(Retreat:progress({}, cleared.ctx), "done", "no zombie observed ends the retreat early")
  local clean = Retreat:verify(nil, nil, {}, cleared.ctx)
  ok(clean ~= nil, "and is a success")
  equal(clean.no_zombie_observed, true, "stated as the observation it is")
end

Harness.group("a stalled retreat opens one readable door, and only with permission")
do
  local squares = {}
  for x = 90, 110 do
    squares[Support.squareKey(x, 200, 0)] = Support.square(x, 200, 0, {})
  end
  local shut = door({ open = false })
  squares[Support.squareKey(100, 200, 0)] = Support.square(100, 200, 0, { shut })
  local s = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, s.ctx), "the blocked retreat validates")
  ok(Retreat:begin({}, s.ctx), "and starts")
  s.ctx.now_ms = Support.NOW
  equal(Retreat:progress({}, s.ctx), "running", "the first poll marks where the gap stood")
  s.ctx.now_ms = Support.NOW + Toolkit.DEFAULT_STALL_MS
  equal(Retreat:progress({}, s.ctx), "running", "the stall is spent on the door, not on failing")
  equal(shut.state.open, true, "which the game's own toggle opened")
  equal(#s.walk.actions, 2, "and the walk was queued again")

  local locked = door({ open = false, locked = true })
  local lockedSquares = {}
  for x = 90, 110 do
    lockedSquares[Support.squareKey(x, 200, 0)] = Support.square(x, 200, 0, {})
  end
  lockedSquares[Support.squareKey(100, 200, 0)] = Support.square(100, 200, 0, { locked })
  local barred = scene({ squares = lockedSquares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  ok(Retreat:validate({}, barred.ctx), "the locked-door retreat validates")
  ok(Retreat:begin({}, barred.ctx), "and starts")
  barred.ctx.now_ms = Support.NOW
  equal(Retreat:progress({}, barred.ctx), "running", "the first poll marks the gap")
  barred.ctx.now_ms = Support.NOW + Toolkit.DEFAULT_STALL_MS
  local outcome, code = Retreat:progress({}, barred.ctx)
  isNil(outcome, "a locked door is not opened")
  equal(code, REASON.DOOR_LOCKED, "and names the reason a key is needed")
  equal(locked.state.open, false, "the door was never toggled")

  local forbidden = scene({ squares = squares, zombies = { Support.zombie({ id = 71, x = 103, y = 200 }) } })
  shut.state.open = false
  ok(Retreat:validate({ allow_doors = false }, forbidden.ctx), "the doorless retreat validates")
  ok(Retreat:begin({ allow_doors = false }, forbidden.ctx), "and starts")
  forbidden.ctx.now_ms = Support.NOW
  equal(Retreat:progress({ allow_doors = false }, forbidden.ctx), "running", "the first poll marks the gap")
  forbidden.ctx.now_ms = Support.NOW + Toolkit.DEFAULT_STALL_MS
  local stuck, stuckCode = Retreat:progress({ allow_doors = false }, forbidden.ctx)
  isNil(stuck, "with doors forbidden the stall is a failure")
  equal(stuckCode, REASON.PATH_STUCK, "with the exact code a doorless walk reports")
  equal(shut.state.open, false, "and the door was left alone")
end

Harness.group("the adapters are registered on the assisted capability, never the autonomous one")
do
  equal(PZ.Adapters.BY_NAME["combat.equip_best"], EquipBest, "equip_best is registered under its action name")
  equal(PZ.Adapters.BY_NAME["combat.shove"], Shove, "and shove")
  equal(PZ.Adapters.BY_NAME["combat.engage"], Engage, "and engage")
  equal(PZ.Adapters.BY_NAME["combat.retreat"], Retreat, "and retreat")
  local adapters = { EquipBest, Shove, Engage, Retreat }
  for index = 1, #adapters do
    local adapter = adapters[index]
    ok(PZ.Protocol.isKnownAction(adapter.name), adapter.name .. " is in the protocol whitelist")
    equal(adapter.capability, "combat_assist", adapter.name .. " rides combat_assist")
    Harness.notEqual(adapter.capability, "autonomous_attack", "and never the capability 12.4 pins shut")
    equal(adapter.experimental, true, adapter.name .. " publishes experimental until a live run proves the presses")
  end
  equal(#(PZ.Adapters.PENDING_PROTOCOL or {}), 0, "no combat action waits outside the protocol whitelist")

  local registry = PZ.CommandDispatcher.new()
  for index = 1, #adapters do
    local registered, reason = registry:register(adapters[index])
    equal(registered, true, adapters[index].name .. " registers: " .. tostring(reason or "accepted"))
  end
end

Harness.group("the declarations are what the dispatcher builds the args from")
do
  local checkArgs = PZ.CommandDispatcher.checkArgs

  local engage = checkArgs(Engage, { target_ref = TARGET }, Support.SESSION)
  equal(engage.target_ref, TARGET, "the target reference survives the rebuild")
  equal(engage.max_swings, Combat.DEFAULT_SWINGS, "and the swing budget defaults below the ceiling")

  local _, swingsCode = checkArgs(Engage, { target_ref = TARGET, max_swings = 5 }, Support.SESSION)
  equal(swingsCode, REASON.INVALID_ARGUMENT, "five swings never reaches the adapter")
  local _, fractionCode = checkArgs(Engage, { target_ref = TARGET, max_swings = 1.5 }, Support.SESSION)
  equal(fractionCode, REASON.INVALID_ARGUMENT, "and neither does half a swing")

  local _, kindCode = checkArgs(Shove, { target_ref = Support.squareRef(1, 2, 0) }, Support.SESSION)
  equal(kindCode, REASON.INVALID_REF, "a square reference is refused at the gate")
  local _, missingCode = checkArgs(Shove, {}, Support.SESSION)
  equal(missingCode, REASON.INVALID_ARGUMENT, "and shoving nothing in particular is not a command")

  local retreat = checkArgs(Retreat, {}, Support.SESSION)
  equal(retreat.distance, Combat.RETREAT_DEFAULT, "retreat defaults to eight squares")
  equal(retreat.allow_doors, true, "doors allowed unless forbidden")
  local _, nearCode = checkArgs(Retreat, { distance = 2 }, Support.SESSION)
  equal(nearCode, REASON.INVALID_ARGUMENT, "a two-square shuffle is below the floor")
  local _, farCode = checkArgs(Retreat, { distance = 16 }, Support.SESSION)
  equal(farCode, REASON.INVALID_ARGUMENT, "and sixteen is past the ceiling")

  local none = checkArgs(EquipBest, {}, Support.SESSION)
  ok(type(none) == "table", "equip_best takes no arguments and accepts none")
  local _, junkCode = checkArgs(EquipBest, { hand = "primary" }, Support.SESSION)
  equal(junkCode, REASON.INVALID_ARGUMENT, "so any argument at all is refused")
end

Harness.finish("adapter_combat")
