--[[
Every adapter the mod ships must actually reach the dispatcher.

This is the test the rest of the Lua suite cannot be: each adapter file has its
own tests, and each of those loads the one adapter it is about and calls it
directly. None of them asks the question that decides whether the mod does
anything at all in a real session -- does PZAgent.ActionRuntime, walking
PZAgent.Adapters.ALL and handing each entry to PZAgent.CommandDispatcher, end up
with a registry that covers the protocol's game actions?

An adapter that is well-written, well-tested and never registered is a mod that
loads cleanly and answers CAPABILITY_UNAVAILABLE to everything.
]]

local ROOT = arg[0]:match("^(.*)test_adapter_registry%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil

local PZ = Harness.loadModules()
dofile(ROOT .. "../../pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
dofile(ROOT .. "../../pz-mod/42/media/lua/client/PZAgent/CommandReader.lua")
dofile(ROOT .. "../../pz-mod/42/media/lua/client/PZAgent/CapabilityRuntime.lua")
dofile(ROOT .. "../../pz-mod/42/media/lua/client/PZAgent/ActionRuntime.lua")

--- Every adapter file in the shipped mod, in the order the engine loads them.
--- Listed explicitly rather than globbed: a file that stops loading should break
--- this test, not silently drop out of the expected set.
local ADAPTER_FILES = {
  "Movement",
  "World",
  "Containers",
  "Doors",
  "Inventory",
  "Consumption",
  "Literature",
  "Equipment",
  "Medical",
  "Rest",
  "Sleep",
  "Combat",
  "Crafting",
  "Building",
}

--- The game actions an adapter file is responsible for. The control plane --
--- session.arm, session.disarm, safety.stop, plan.cancel, action.wait -- is
--- served by PZAgent.ActionRuntime itself and is checked separately below.
local GAME_ACTIONS = {
  "movement.move_to",
  "movement.move_near",
  "world.inspect",
  "container.inspect",
  "container.open_nearby",
  "door.open",
  "door.close",
  "door.unlock",
  "inventory.search",
  "inventory.transfer",
  "inventory.transfer_batch",
  "inventory.ensure_main",
  "consume.eat",
  "consume.drink",
  "consume.drink_source",
  "literature.read",
  "equipment.equip",
  "equipment.unequip",
  "medical.bandage",
  "survival.rest",
  "survival.sleep",
  "combat.equip_best",
  "combat.shove",
  "combat.engage",
  "combat.retreat",
  "crafting.inspect",
  "crafting.craft",
  "building.inspect",
  "building.build",
}

local CONTROL_ACTIONS = {
  "session.arm",
  "session.disarm",
  "safety.stop",
  "plan.cancel",
  "action.wait",
}

Support.loadModules(Harness.root, ADAPTER_FILES)

print("- every shipped adapter declares itself the way the dispatcher demands")

local published = PZ.Adapters.ALL
ok(type(published) == "table", "the adapters publish themselves into a registry")
ok(#published > 0, "the registry is not empty")

--- The dispatcher reads `adapter.action`; an adapter that names itself under any
--- other key is invisible to it. This ran red first: eight of the ten files
--- published a `name` instead, so they registered nowhere and every game action
--- answered CAPABILITY_UNAVAILABLE in a mod that otherwise looked healthy.
for index = 1, #published do
  local adapter = published[index]
  ok(
    type(adapter.action) == "string",
    string.format("registry entry %d names its action under `action`", index)
  )
  ok(
    type(adapter.start) == "function",
    string.format("registry entry %d (%s) provides start()", index, tostring(adapter.action))
  )
end

print("- the registry covers every game action in the protocol")

local byAction = {}
for index = 1, #published do
  local adapter = published[index]
  if type(adapter.action) == "string" then
    ok(byAction[adapter.action] == nil, "no two adapters claim " .. adapter.action)
    byAction[adapter.action] = adapter
  end
end

for index = 1, #GAME_ACTIONS do
  local action = GAME_ACTIONS[index]
  ok(byAction[action] ~= nil, "an adapter is published for " .. action)
end

print("- the dispatcher accepts every published adapter")

local dispatcher = PZ.CommandDispatcher.new()
for index = 1, #published do
  local adapter = published[index]
  local registered, reason = dispatcher:register(adapter)
  ok(
    registered == true,
    string.format(
      "the dispatcher registers %s (%s)",
      tostring(adapter.action),
      tostring(reason or "accepted")
    )
  )
end

print("- installing the runtime yields a registry that answers for every action")

-- A real PZAgent.Ipc over a mock filesystem rather than a hand-written double.
-- The install path publishes a capability report through it, and a double that
-- happened to implement only the methods this test expected would pass while the
-- real one raised -- which is what happened on the first run of this file.
local Mock = dofile(ROOT .. "support/mock_game.lua")
local CommandSupport = dofile(ROOT .. "support/command_support.lua")

local agent = CommandSupport.agent(Mock)
local runtime, installError = PZ.ActionRuntime.install(agent, CommandSupport.NOW, {})
ok(runtime ~= nil, "the runtime installs: " .. tostring(installError or "ok"))

if runtime ~= nil then
  local registry = agent.dispatcher
  ok(registry ~= nil, "the install leaves its dispatcher on the agent")
  if registry ~= nil then
    local names = registry:registered()
    local covered = {}
    for index = 1, #names do
      covered[names[index]] = true
    end

    for index = 1, #GAME_ACTIONS do
      local action = GAME_ACTIONS[index]
      ok(covered[action] == true, "the installed registry answers for " .. action)
    end

    for index = 1, #CONTROL_ACTIONS do
      local action = CONTROL_ACTIONS[index]
      ok(covered[action] == true, "the installed registry answers for " .. action)
    end

    equal(
      #names,
      #GAME_ACTIONS + #CONTROL_ACTIONS,
      "the registry holds exactly the protocol's actions and nothing else"
    )
  end
end

print("- nothing published claims an action outside the whitelist")

local pending = PZ.Adapters.PENDING_PROTOCOL or {}
equal(#pending, 0, "no adapter claims an action the protocol does not know")

print("- the capabilities that carry an experimental ceiling are published as experimental")

-- CapabilityRuntime reads `adapter.experimental` and publishes EXPERIMENTAL
-- instead of AVAILABLE_UNVERIFIED when it is set. Toolkit.declare carried no
-- such field, so the flag was read here and written nowhere: two adapters had
-- comments saying "the probe caps this at experimental" and both published as
-- ordinary unverified, while docs/PROTOCOL.md documents capabilities.json with
-- an example showing a state its own writer could not emit.
--
-- Asserted against the capability names rather than the adapters, because what
-- an operator reads is the published report and the report is keyed that way.
--
-- `combat_assist` joins the two 12.4 caps for its own stated reason: the shove
-- and attack press spellings are the least certain engine assumptions in the
-- mod (docs/GAME_API_VERIFICATION.md says so, row by row), so the assisted
-- combat rung must not publish as ordinary unverified until a live run proves
-- them. It is a different capability from `autonomous_attack`, whose ceiling
-- stays "unsupported by design" and which no adapter declares -- the pin at
-- the end of this file fails if one ever does.
--
-- `crafting` joins them on two reasons at once. Build 42 rewrote crafting and
-- none of the recipe spellings in adapters/Crafting.lua has been seen
-- answering in a live session; and a craft consumes materials that cannot be
-- put back, so the tools stay withheld on every install until a live run
-- observes one.
--
-- `building` joins them on a stronger version of the same second reason. A
-- craft spends materials the character owned; a build leaves a structure
-- standing in the world, and this mod ships no action that takes one down. The
-- capability therefore stays experimental -- `building.build` is withheld on
-- every install until a live build promotes it -- and even promoted it is a P4
-- action, which no mode gives an autonomous path to.
local EXPERIMENTAL_CAPABILITIES = {
  survival_sleep = true,
  drink_world_source = true,
  combat_assist = true,
  crafting = true,
  building = true,
}

local declared = {}
for index = 1, #published do
  local adapter = published[index]
  if adapter.capability ~= nil then
    declared[adapter.capability] = adapter.experimental == true
  end
end

for capability in pairs(EXPERIMENTAL_CAPABILITIES) do
  ok(
    declared[capability] == true,
    capability .. " declares itself experimental, so the runtime can publish that state"
  )
end

for capability, isExperimental in pairs(declared) do
  if not EXPERIMENTAL_CAPABILITIES[capability] then
    ok(
      isExperimental == false,
      capability .. " does not claim an experimental ceiling it has no reason for"
    )
  end
end

print("- no adapter touches the capability 12.4 pins shut")

-- The assisted combat rung rides `combat_assist`. `autonomous_attack` stays
-- what it has always been -- unsupported by design, implemented by nothing --
-- and this is the pin that keeps an adapter from quietly claiming it.
isNil(declared["autonomous_attack"], "nothing published declares autonomous_attack")

print("- every adapter names the engine symbols it needs")

for index = 1, #published do
  local adapter = published[index]
  local symbols = adapter.required_symbols or adapter.requires or {}
  ok(
    type(symbols) == "table",
    string.format("%s declares its engine symbols as a table", tostring(adapter.action))
  )
end

Harness.finish("adapter_registry")
