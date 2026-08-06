-- Tests for PZAgent.CommandDispatcher: the gate a payload has to get through
-- before an adapter exists as far as it is concerned.
--
-- The central assertion of this file is negative and is made with a counting
-- adapter: for every crafted payload, `spy.starts` is still zero afterwards. A
-- test that only checked the returned reason code would pass just as happily
-- against a dispatcher that called the adapter first and refused afterwards.

local ROOT = arg[0]:match("^(.*)test_command_dispatcher%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Mock = dofile(ROOT .. "support/mock_game.lua")
local Support = dofile(ROOT .. "support/command_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local Dispatcher = PZ.CommandDispatcher
local Protocol = PZ.Protocol
local REASON = Protocol.REASON
local ARG = Dispatcher.ARG

local equal, ok, isNil, contains, same = Harness.equal, Harness.ok, Harness.isNil, Harness.contains, Harness.same

local SESSION = Support.SESSION

local function itemRef(sessionId)
  local ref, err = PZ.Refs.buildItem(sessionId or SESSION, "player-main", "1234", 0)
  if ref == nil then
    error("could not build a test item ref: " .. tostring(err), 0)
  end
  return ref
end

Harness.group("registration refuses a declaration it cannot honour")
do
  local registry = Dispatcher.new()
  local cases = {
    { adapter = "movement.move_to", needle = "must be a table" },
    { adapter = { action = "movement.teleport", start = function() end }, needle = "whitelist" },
    { adapter = { action = "movement.move_to" }, needle = "start()" },
    {
      adapter = { action = "movement.move_to", start = function() end, poll = "later" },
      needle = "poll must be a function",
    },
    {
      adapter = { action = "movement.move_to", start = function() end, required_symbols = { "os.execute()" } },
      needle = "plain engine symbol",
    },
    {
      adapter = {
        action = "movement.move_to",
        start = function() end,
        args = { square = { type = "square" } },
      },
      needle = "unknown type",
    },
    {
      adapter = {
        action = "movement.move_to",
        start = function() end,
        args = { square = { type = ARG.REF } },
      },
      needle = "accepted ref kinds",
    },
    {
      adapter = {
        action = "movement.move_to",
        start = function() end,
        args = { mode = { type = ARG.ENUM } },
      },
      needle = "no values",
    },
  }
  for index = 1, #cases do
    local registered, reason = registry:register(cases[index].adapter)
    isNil(registered, "declaration " .. index .. " is refused")
    contains(reason, cases[index].needle, "and the reason names the problem")
  end
end

Harness.group("an action may only be claimed once")
do
  local registry = Dispatcher.new()
  ok(registry:register(Support.spyAdapter("movement.move_to")), "the first adapter registers")
  local second, reason = registry:register(Support.spyAdapter("movement.move_to"))
  isNil(second, "the second is refused")
  contains(reason, "already registered", "and the reason says why")
  same(registry:registered(), { "movement.move_to" }, "the registry still holds one adapter")
end

Harness.group("an action with no adapter is CAPABILITY_UNAVAILABLE, naming the action")
do
  local registry = Dispatcher.new()
  local adapter, reasonCode, detail = registry:resolve({ action = "consume.eat", args = {} }, SESSION)
  isNil(adapter, "nothing is resolved")
  equal(reasonCode, REASON.CAPABILITY_UNAVAILABLE, "the reason is a missing capability")
  contains(detail, "consume.eat", "and the detail names the action")
end

Harness.group("a crafted payload never reaches an adapter")
do
  local spy = Support.spyAdapter("inventory.transfer", {
    args = {
      item = { type = ARG.REF, required = true, kinds = { item = true } },
      count = { type = ARG.NUMBER, integer = true, min = 1, max = 10 },
    },
  })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "the adapter is registered")

  local crafted = {
    {
      name = "an action name that is a Lua expression",
      command = { action = "PZAgent.Ipc.writeRaw", args = {} },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a chunk of Lua under an undeclared key",
      command = {
        action = "inventory.transfer",
        args = { item = itemRef(), code = "os.execute('del *.*')" },
      },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a file path where a token belongs",
      command = {
        action = "inventory.transfer",
        args = { item = "../../../autoexec.lua" },
      },
      reason = REASON.INVALID_REF,
    },
    {
      name = "a Java class name where a ref belongs",
      command = { action = "inventory.transfer", args = { item = "zombie.inventory.ItemContainer" } },
      reason = REASON.INVALID_REF,
    },
    {
      name = "a ref minted by another session",
      command = { action = "inventory.transfer", args = { item = itemRef(Support.OTHER_SESSION) } },
      reason = REASON.INVALID_REF,
    },
    {
      name = "a nested table",
      command = { action = "inventory.transfer", args = { item = { ref = itemRef() } } },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a missing required argument",
      command = { action = "inventory.transfer", args = { count = 1 } },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a count outside its declared bounds",
      command = { action = "inventory.transfer", args = { item = itemRef(), count = 11 } },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a fractional count",
      command = { action = "inventory.transfer", args = { item = itemRef(), count = 1.5 } },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a count that is not a number at all",
      command = { action = "inventory.transfer", args = { item = itemRef(), count = "1" } },
      reason = REASON.INVALID_ARGUMENT,
    },
    {
      name = "a null argument",
      command = { action = "inventory.transfer", args = { item = itemRef(), count = PZ.Json.null } },
      reason = REASON.INVALID_ARGUMENT,
    },
  }
  for index = 1, #crafted do
    local case = crafted[index]
    local adapter, reasonCode = registry:resolve(case.command, SESSION)
    isNil(adapter, case.name .. " resolves to no adapter")
    equal(reasonCode, case.reason, case.name .. " is refused with the right reason")
  end
  equal(spy.starts, 0, "the adapter was never called")
  isNil(spy.last_args, "and never saw an argument table")
end

Harness.group("more arguments than the cap are refused before they are checked")
do
  local spy = Support.spyAdapter("world.inspect", {
    args = { note = { type = ARG.STRING } },
  })
  local registry = Dispatcher.new()
  registry:register(spy)
  local args = {}
  for index = 1, Dispatcher.MAX_ARGS + 1 do
    args["k" .. index] = "v"
  end
  local adapter, reasonCode, detail = registry:resolve({ action = "world.inspect", args = args }, SESSION)
  isNil(adapter, "the payload is refused")
  equal(reasonCode, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  ok(detail ~= nil, "and a detail")
  equal(spy.starts, 0, "the adapter was never called")
end

Harness.group("a declared string may not carry anything a reader could interpret")
do
  local spy = Support.spyAdapter("world.inspect", {
    args = { note = { type = ARG.STRING } },
  })
  local registry = Dispatcher.new()
  registry:register(spy)
  local rejected = {
    "with space",
    "quote\"inside",
    "call()",
    "slash/inside",
    "back\\slash",
    "new\nline",
    string.rep("x", Dispatcher.MAX_STRING_BYTES + 1),
    "",
  }
  for index = 1, #rejected do
    local adapter, reasonCode = registry:resolve({
      action = "world.inspect",
      args = { note = rejected[index] },
    }, SESSION)
    isNil(adapter, string.format("string %d is refused", index))
    equal(reasonCode, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  end
  local adapter, args = registry:resolve({ action = "world.inspect", args = { note = "kitchen-1" } }, SESSION)
  ok(adapter ~= nil, "a plain token is accepted")
  equal(args.note, "kitchen-1", "and reaches the adapter unchanged")
end

Harness.group("the table an adapter receives is rebuilt from the declaration")
do
  local spy = Support.spyAdapter("consume.eat", {
    args = {
      item = { type = ARG.REF, required = true, kinds = { item = true } },
      fraction = { type = ARG.NUMBER, min = 0, max = 1, default = 0.5 },
      force = { type = ARG.BOOLEAN },
    },
  })
  local registry = Dispatcher.new()
  registry:register(spy)
  local ref = itemRef()
  local adapter, args = registry:resolve({ action = "consume.eat", args = { item = ref } }, SESSION)
  ok(adapter ~= nil, "the command resolves")
  same(args, { item = ref, fraction = 0.5 }, "the declared default is filled in and nothing else is present")

  local _, withBoolean = registry:resolve({
    action = "consume.eat",
    args = { item = ref, force = true, fraction = 0.25 },
  }, SESSION)
  same(withBoolean, { item = ref, force = true, fraction = 0.25 }, "declared values pass through")
end

Harness.group("a ref of the wrong kind is refused")
do
  local spy = Support.spyAdapter("movement.move_to", {
    args = { square = { type = ARG.REF, required = true, kinds = { square = true } } },
  })
  local registry = Dispatcher.new()
  registry:register(spy)
  local adapter, reasonCode, detail = registry:resolve({
    action = "movement.move_to",
    args = { square = itemRef() },
  }, SESSION)
  isNil(adapter, "an item ref is not a square ref")
  equal(reasonCode, REASON.INVALID_REF, "and it is INVALID_REF")
  contains(detail, "item", "the detail names the kind that arrived")

  local square = PZ.Refs.buildSquare(SESSION, 10, 20, 0)
  local resolved, args = registry:resolve({ action = "movement.move_to", args = { square = square } }, SESSION)
  ok(resolved ~= nil, "the right kind resolves")
  equal(args.square, square, "and the ref is passed through verbatim")
end

Harness.group("a command that is not an object is refused")
do
  local registry = Dispatcher.new()
  local adapter, reasonCode = registry:resolve("action.wait", SESSION)
  isNil(adapter, "a string is not a command")
  equal(reasonCode, REASON.INVALID_ARGUMENT, "and it is INVALID_ARGUMENT")
end

Harness.group("every protocol action is either registered or unavailable, never unknown")
do
  local agent, _, _, registry = Support.runtime(Mock, {}, { controls = true })
  ok(agent ~= nil, "the runtime installed")
  for index = 1, #Protocol.ACTION_NAMES do
    local action = Protocol.ACTION_NAMES[index]
    local registered = registry:adapterFor(action)
    local adapter, reasonCode = registry:resolve({ action = action, args = {} }, SESSION)
    if registered == nil then
      isNil(adapter, action .. " resolves to nothing")
      equal(reasonCode, REASON.CAPABILITY_UNAVAILABLE,
        action .. " has no adapter, and says so with CAPABILITY_UNAVAILABLE")
    elseif adapter == nil then
      -- Registered, but its arguments were not supplied: that is a complaint
      -- about the command, never about the build.
      equal(reasonCode, REASON.INVALID_ARGUMENT, action .. " has an adapter and wanted arguments")
    else
      equal(adapter.action, action, action .. " resolves to its own adapter")
    end
  end
end

Harness.finish("test_command_dispatcher")
