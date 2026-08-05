-- PZAgent.Observe: the half that touches the game.
--
-- The doubles in support/observe_support.lua expose exactly the accessors a
-- test names, so removing one is how "this build does not have that method" is
-- expressed. Every group below is therefore about the same question: when the
-- engine does not answer, does the reader leave the field out, or does it make
-- one up?

local Harness = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/mock_game.lua")
local Support = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/observe_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local Observe = PZ.Observe
local Model = PZ.ObserveModel
local Json = PZ.Json

local equal, ok, isNil, same = Harness.equal, Harness.ok, Harness.isNil, Harness.same

local NOW = 1700000000000
local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

--- Install a build probe, since PZAgent.Heartbeat reads the version from the
--- game and an observation without a build is refused.
local function installCore(version)
  local core = {
    getVersionNumber = function()
      return version
    end,
  }
  getCore = function()
    return core
  end
  return function()
    getCore = nil
  end
end

--- An agent over a mock filesystem, with an accepted session.
local function newAgent(options)
  options = options or {}
  local fs = Mock.newFilesystem()
  local agent = {
    ipc = PZ.Ipc.new({ fileApi = fs.api, clock = function()
      return NOW
    end }),
    safety = PZ.Safety.newState(),
    session = PZ.Session.new(),
    sequence = PZ.Sequence.new(),
  }
  if options.session ~= false then
    agent.session:offer({
      protocol_version = "1.0",
      session_id = SESSION,
      created_at_ms = NOW - 10,
      nonce = "nonce-one",
      mode = "observe",
    }, NOW)
  end
  return agent, fs
end

--- A player with an inventory, a worn bag, wounds and moodles.
local function furnishedPlayer()
  local bottle = Support.item({ id = 5, full_type = "Base.WaterBottleFull", name = "Water Bottle", weight = 0.8 })
  local satchel = Support.item({
    id = 99,
    full_type = "Base.Bag_Satchel",
    name = "Satchel",
    weight = 1,
    contents = { bottle },
  })
  local apple = Support.item({
    id = 1,
    full_type = "Base.Apple",
    name = "Apple",
    category = "Food",
    weight = 0.3,
    hunger_change = -15,
    cooked = false,
  })
  local backpack = Support.item({ id = 77, full_type = "Base.Bag_BigHikingBag", name = "Big Hiking Bag", contents = {
    Support.item({ id = 8, full_type = "Base.Nails", name = "Nails", weight = 0.1 }),
  } })
  return Support.player({
    x = 100.5,
    y = 200.5,
    z = 0,
    angle = 90,
    stats = Support.stats({ hunger = 0.4, thirst = 0.1, endurance = 0.9 }),
    body = Support.bodyDamage({
      Support.bodyPart({ part = "Left_Arm", bleeding = true, bitten = true, health = 40 }),
      Support.bodyPart({ part = "Torso", health = 100 }),
    }, 78),
    moodles = Support.moodles({ { "Hungry", 2 }, { "Bored", 1 } }),
    inventory = Support.container({ apple, satchel }),
    worn = { Support.worn("Back", backpack) },
    primary = bottle,
    can_see = true,
  }), bottle
end

Harness.group("outside the game every probe reports its own absence")
do
  local fields = Observe.gameFields()
  isNil(fields.build, "with no getCore there is no build to report")
  isNil(fields.save_key, "with no getWorld there is no save identity")
  isNil(fields.speed, "and no clock to read a speed from")

  isNil(Observe.positionOf(nil), "an object that is not there has no position")
  local playerFields, reason = Observe.playerFields({})
  isNil(playerFields, "a character that reports no coordinates yields no player block")
  Harness.contains(reason, "position", "and the reason says so")

  same(Observe.playerStats({}), {}, "a character with no stats object contributes no stats")
  same(Observe.playerMoodles({}), {}, "and no moodles")
  same(Observe.playerWounds({}), {}, "and no wounds")

  local roots, rootsReason = Observe.inventoryRoots({})
  isNil(roots, "a character with no inventory yields no container roots")
  Harness.contains(rootsReason, "inventory", "and the reason names it")

  local nearby = Observe.nearbyObjects({ x = 0, y = 0, z = 0 })
  same(nearby.objects, {}, "with no cell there is nothing nearby")
  equal(nearby.truncated, false, "and nothing was cut short either")
end

Harness.group("the game block reports what the engine actually answered")
do
  local removeCore = installCore("42.20")
  local removeWorld = Support.installWorld("Muldraugh, KY/survivor")
  local removeTime = Support.installGameTime({ speed = 2, hour = 13, minute = 45, paused = false })

  local fields = Observe.gameFields()
  equal(fields.build, "42.20", "the build comes from the game")
  equal(fields.save_key, "Muldraugh, KY/survivor", "the save key is read whole")
  equal(fields.speed, 2, "the speed multiplier is read")
  equal(fields.paused, false, "and the pause flag")
  equal(fields.world_time, "1993-06-09T13:45", "the world clock is formatted from the components it read")

  local game = Model.game(fields)
  ok(game.save_id:match("^%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x$") ~= nil, "and only its hash reaches the document")
  Harness.notEqual(game.save_id, Model.saveId("Muldraugh, KY/other"), "two saves hash differently")

  removeTime()
  Support.installGameTime({ speed = 0 })
  local stopped = Observe.gameFields()
  equal(stopped.paused, true, "with no pause accessor a zero multiplier is the game's own reading of paused")

  getGameTime = nil
  local blind = Observe.gameFields()
  isNil(blind.paused, "with no clock at all nothing is claimed about the pause state")

  removeWorld()
  removeCore()
end

Harness.group("the player block carries the scalars and omits the rest")
do
  local player = furnishedPlayer()
  local fields = Observe.playerFields(player)
  equal(fields.present, true, "the character is present")
  equal(fields.alive, true, "and alive, because isDead answered")
  equal(fields.position.x, 100.5, "x")
  equal(fields.position.z, 0, "the floor is an integer")
  equal(fields.position.direction, "E", "ninety degrees is east")
  equal(fields.stats.hunger, 0.4, "hunger as the game reports it")
  equal(fields.stats.health, 0.78, "overall body health scaled out of a hundred")
  isNil(fields.stats.fatigue, "a stat the Stats object does not expose is absent")
  equal(fields.moodles.Hungry, 2, "moodles are keyed by their type name")

  local dead = Observe.playerFields(Support.player({ dead = true }))
  equal(dead.alive, false, "a dead character says so")

  local mute = Observe.playerFields({
    getX = function() return 1 end,
    getY = function() return 2 end,
    getZ = function() return 0 end,
  })
  equal(mute.alive, false, "a character whose liveness cannot be read is not assumed to be alive")
  isNil(mute.position.direction, "and its facing is omitted")
end

Harness.group("a bleeding limb reaches the document, because the guard keys on it")
do
  local player = furnishedPlayer()
  local wounds = Observe.playerWounds(player)
  equal(#wounds, 2, "every body part is described")
  equal(wounds[1].part, "Left_Arm", "the part names itself")
  equal(wounds[1].bleeding, true, "and reports that it is bleeding")
  equal(wounds[1].bitten, true, "and that it was bitten")
  ok(wounds[1].severity > 0.5, "with a severity derived from its health")

  local shaped = Model.wounds(SESSION, wounds, { wounds_truncated = false, wounds_omitted = 0 })
  equal(#shaped, 1, "the undamaged part produces no wound")
  equal(shaped[1].kind, "bite", "the bite names the wound")
  equal(shaped[1].bleeding, true, "and the bleeding survives into the document")

  local unnamed = Observe.playerWounds(Support.player({
    body = Support.bodyDamage({ Support.bodyPart({ health = 10 }) }),
  }))
  equal(unnamed[1].part, "part-0", "a part whose type cannot be read is identified by position, not by a guess")
end

Harness.group("the inventory walk nests, names its slots and finds the hands")
do
  local player, bottle = furnishedPlayer()
  local roots = Observe.inventoryRoots(player)
  equal(#roots, 2, "the main inventory and the worn bag are separate roots")
  equal(roots[1].kind, "player_main", "the first root is the main inventory")
  equal(roots[2].kind, "worn", "the worn bag is a worn container")
  equal(roots[2].slot, "Back", "carrying the slot it is worn in")
  equal(roots[2].runtime_id, 77, "and the id of the item that provides it")

  local satchel = roots[1].items[2]
  equal(satchel.container.kind, "carried", "a bag in the inventory is a carried container")
  equal(satchel.container.runtime_id, 99, "identified by the bag item")
  equal(satchel.container.items[1].hand, "primary", "the item in the primary hand is marked wherever it lives")
  ok(rawequal(bottle, bottle), "the hand is matched by identity, not by name")

  equal(roots[1].items[1].food.hunger_change, -15, "a food item carries its domain payload")
  isNil(roots[1].items[2].food, "and an item that is not food carries none")

  local document = Model.build({
    session_id = SESSION,
    seq = 0,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    inventory = roots,
  })
  local containers = document.inventory.containers
  equal(#containers, 3, "three containers reach the document")
  equal(containers[1].ref, "container:" .. SESSION .. ":carried:99", "the nested bag")
  equal(containers[1].parent_ref, "container:" .. SESSION .. ":player-main", "whose parent is the main inventory")
  equal(document.player.hands.primary, "item:" .. SESSION .. ":carried:99:5:0", "and the held bottle is referenced")

  local many = {}
  for index = 1, Model.MAX_ITEMS_PER_CONTAINER + 5 do
    many[index] = Support.item({ id = index, name = "Nail" })
  end
  local bounded = Support.container(many)
  local node = Observe.inventoryRoots(Support.player({ inventory = bounded }))[1]
  equal(#node.items, Model.MAX_ITEMS_PER_CONTAINER, "the reader stops at the cap")
  equal(node.truncated, true, "and declares that it did")
  equal(node.dropped, 5, "with the number it did not look at")
end

Harness.group("nearby distinguishes what it saw from what it could not tell")
do
  local player = furnishedPlayer()
  local position = { x = 100, y = 200, z = 0 }
  local squares = {
    ["102,200,0"] = Support.square({ Support.worldObject({ name = "Fridge", container_type = "fridge" }) }),
    ["100,203,0"] = Support.square({ Support.worldObject({ name = "Door" }) }),
    ["101,200,0"] = Support.square({ Support.worldObject({ name = "Sink", water = 40 }) }),
  }
  local zombies = {
    Support.zombie({ id = 42, x = 104, y = 200, has_target = true, target = player }),
    Support.zombie({ id = 43, x = 101, y = 200, has_target = true }),
    Support.zombie({ id = 44, x = 300, y = 200, has_target = true }),
    Support.zombie({ id = 45, x = 102, y = 200 }),
  }
  local removeCell = Support.installCell(squares, zombies)

  local objects = Observe.nearbyObjects(position)
  equal(#objects.objects, 3, "every named object inside the radius is reported")
  equal(objects.truncated, false, "and nothing was cut short")

  local nearbyZombies = Observe.nearbyZombies(player, position)
  equal(#nearbyZombies.zombies, 3, "a zombie beyond the radius is not reported at all")
  equal(nearbyZombies.zombies[1].chasing, true, "a zombie whose target is the player is chasing")
  equal(nearbyZombies.zombies[2].chasing, false, "a zombie with no target is not")
  isNil(nearbyZombies.zombies[3].chasing, "and one whose target cannot be read makes no claim either way")
  equal(nearbyZombies.zombies[1].visible, true, "visibility comes from the character's own line of sight")

  local document = Model.build({
    session_id = SESSION,
    seq = 1,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    nearby = Observe.nearbyFields(player, position),
  })
  local emitted = document.nearby.objects
  local byKind = {}
  for index = 1, #emitted do
    byKind[emitted[index].kind] = emitted[index]
  end
  local fridgeRef = "container:" .. SESSION .. ":world:102:200:0:0:0"
  equal(byKind.fridge.ref, fridgeRef, "a container is referenced as a container")
  same(byKind.fridge.semantics, { "container" }, "and says so in its semantics")
  equal(byKind.door.ref, "square:" .. SESSION .. ":100:203:0", "everything else is referenced by its square")
  same(byKind.door.semantics, { "door", "obstacle" }, "with the semantics its kind implies")
  same(byKind.sink.semantics, { "water_source" }, "a water source is flagged for the drink policy")

  equal(
    document.player.stats[Model.LIMIT_PREFIX .. "chasing_unknown"],
    true,
    "the zombie nobody could read is declared, because an absent chasing flag reads as safe"
  )
  removeCell()
end

Harness.group("a tick publishes a snapshot, or says why it did not")
do
  local removeCore = installCore("42.20")
  local removeWorld = Support.installWorld("Muldraugh, KY/survivor")
  local removeTime = Support.installGameTime({ speed = 1, paused = false })
  local removeCell = Support.installCell({}, {})

  local agent, fs = newAgent()
  agent.player = furnishedPlayer()
  agent.queue_description = PZ.Ownership.describe({}, SESSION)

  local document, err = Observe.tick(agent, NOW)
  ok(document ~= nil, "the tick produced a document: " .. tostring(err))
  equal(document.seq, 0, "numbered from the observation stream")
  equal(document.session_id, SESSION, "and stamped with the session")

  local pointer = Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_pointer")))
  equal(pointer.slot, "a", "the first snapshot lands in slot a")
  equal(pointer.seq, 0, "and the pointer names its sequence")
  local slot = Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_a")))
  equal(slot.player.stats.hunger, 0.4, "the published document is the one that was built")
  ok(
    fs:lastWriteIndex(PZ.Ipc.pathFor("snapshot_pointer")) > fs:lastWriteIndex(PZ.Ipc.pathFor("snapshot_a")),
    "and the pointer is written after the slot it commits"
  )

  local second = Observe.tick(agent, NOW + 1000)
  equal(second.seq, 1, "the next tick advances the observation sequence")
  equal(Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_pointer"))).slot, "b", "and alternates slots")

  local bagless, baglessFs = newAgent()
  bagless.player = Support.player({ x = 1, y = 2, z = 0 })
  local partial = Observe.tick(bagless, NOW)
  ok(partial ~= nil, "a character whose inventory cannot be read still produces a snapshot")
  isNil(partial.inventory, "with no inventory section, rather than an empty one")
  Harness.contains(bagless.safety.last_error, "inventory", "and the reason is still surfaced")
  ok(baglessFs:read(PZ.Ipc.pathFor("snapshot_a")) ~= nil, "and the snapshot was published")

  local sessionless = newAgent({ session = false })
  sessionless.player = furnishedPlayer()
  local none, sessionReason = Observe.tick(sessionless, NOW)
  isNil(none, "with no session nothing is published")
  Harness.contains(sessionReason, "session", "and the reason says why")
  isNil(sessionless.safety.last_error, "which is not an error worth showing the player")

  local blind = newAgent()
  blind.player = { getX = function() return 1 end }
  local nothing, blindReason = Observe.tick(blind, NOW)
  isNil(nothing, "a character the reader cannot place produces no snapshot")
  equal(blind.safety.last_error, blindReason, "and the failure reaches the HUD instead of passing silently")

  local unwritable, unwritableFs = newAgent()
  unwritable.player = furnishedPlayer()
  unwritableFs:failWritesTo(PZ.Ipc.pathFor("snapshot_a"))
  local unpublished = Observe.tick(unwritable, NOW)
  isNil(unpublished, "a snapshot that could not be written is not reported as published")
  Harness.contains(unwritable.safety.last_error, "snapshot", "and the disk failure is recorded")

  removeCell()
  removeTime()
  removeWorld()
  removeCore()
end

Harness.finish("observe")
