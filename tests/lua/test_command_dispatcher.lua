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

Harness.group("a bound that cannot be compared is refused at registration, not at dispatch")
do
  -- The bounds a declaration carries are only ever used in a comparison against
  -- the value that arrived: `#value > spec.max_bytes`, `value < spec.min`. One
  -- that is not a number raises there -- inside PZAgent.ActionRuntime's
  -- admission path, on the first command of the session, for every command of
  -- that action afterwards. This file's contract is that a malformed
  -- declaration is caught at load, so the registration is what has to refuse.
  local cases = {
    {
      args = { mode = { type = ARG.STRING, max_bytes = "sixty" } },
      value = "walk",
      needle = "max_bytes",
    },
    {
      args = { radius = { type = ARG.NUMBER, min = "one" } },
      value = 4,
      needle = "min",
    },
    {
      args = { radius = { type = ARG.NUMBER, max = {} } },
      value = 4,
      needle = "max",
    },
  }
  for index = 1, #cases do
    local registry = Dispatcher.new()
    local spy = Support.spyAdapter("movement.move_to", { args = cases[index].args })
    local registered, reason = registry:register(spy)
    isNil(registered, "declaration " .. index .. " is refused at registration")
    contains(reason, cases[index].needle, "and the reason names the bound that cannot be compared")
    equal(spy.starts, 0, "no adapter was reached")
  end

  -- And the numeric bounds every shipped adapter actually declares still pass.
  local registry = Dispatcher.new()
  local sound = Support.spyAdapter("movement.move_to", {
    args = {
      radius = { type = ARG.NUMBER, min = 0.1, max = 32 },
      mode = { type = ARG.STRING, max_bytes = 16 },
    },
  })
  ok(registry:register(sound), "a declaration whose bounds are numbers still registers")
  local adapter, args = registry:resolve({ action = "movement.move_to", args = { radius = 4, mode = "walk" } }, SESSION)
  ok(adapter ~= nil, "and it resolves a command")
  equal(type(args) == "table" and args.radius or nil, 4, "with the value it declared bounds for")
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

-- ---------------------------------------------------------------------------
-- LIST arguments
-- ---------------------------------------------------------------------------

local notEqual = Harness.notEqual

--- A distinct valid item ref per `index`, so a list test can tell its elements
--- apart and a duplicate is something the test placed, never an accident.
local function listItemRef(index, sessionId)
  local ref, err = PZ.Refs.buildItem(sessionId or SESSION, "player-main", tostring(1000 + index), 0)
  if ref == nil then
    error("could not build a test item ref: " .. tostring(err), 0)
  end
  return ref
end

Harness.group("a list declaration is checked at registration, not at dispatch")
do
  local function listAdapter(args)
    return { action = "movement.move_to", start = function() end, args = args }
  end
  local cases = {
    {
      name = "a list without an element kind",
      args = { items = { type = ARG.LIST, max_items = 4, kinds = { item = true } } },
      needle = "ref or string",
    },
    {
      name = "a list of numbers",
      args = { items = { type = ARG.LIST, of = ARG.NUMBER, max_items = 4 } },
      needle = "ref or string",
    },
    {
      name = "a list of lists",
      args = { items = { type = ARG.LIST, of = ARG.LIST, max_items = 4 } },
      needle = "ref or string",
    },
    {
      name = "a list without max_items",
      args = { items = { type = ARG.LIST, of = ARG.STRING } },
      needle = "max_items in 1..8",
    },
    {
      name = "a zero max_items",
      args = { items = { type = ARG.LIST, of = ARG.STRING, max_items = 0 } },
      needle = "max_items in 1..8",
    },
    {
      name = "a max_items above the ceiling",
      args = {
        items = { type = ARG.LIST, of = ARG.STRING, max_items = Dispatcher.MAX_LIST_ITEMS + 1 },
      },
      needle = "max_items in 1..8",
    },
    {
      name = "a fractional max_items",
      args = { items = { type = ARG.LIST, of = ARG.STRING, max_items = 2.5 } },
      needle = "max_items in 1..8",
    },
    {
      name = "a max_items that is not a number",
      args = { items = { type = ARG.LIST, of = ARG.STRING, max_items = "4" } },
      needle = "max_items in 1..8",
    },
    {
      name = "a ref list without kinds",
      args = { items = { type = ARG.LIST, of = ARG.REF, max_items = 4 } },
      needle = "ref kinds",
    },
    {
      name = "an argument type the dispatcher does not know",
      args = { items = { type = "list-of-refs" } },
      needle = "unknown type",
    },
  }
  for index = 1, #cases do
    local registry = Dispatcher.new()
    local registered, reason = registry:register(listAdapter(cases[index].args))
    isNil(registered, cases[index].name .. " is refused")
    contains(reason, cases[index].needle, "and the reason names the problem")
  end
end

Harness.group("a dense array of valid refs passes, element-checked and rebuilt")
do
  local spy = Support.spyAdapter("inventory.transfer_batch", {
    args = {
      item_refs = {
        type = ARG.LIST, of = ARG.REF, required = true, max_items = 4, kinds = { item = true },
      },
    },
  })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "the list adapter registers")
  local supplied = { listItemRef(1), listItemRef(2), listItemRef(3) }
  local adapter, args = registry:resolve({
    action = "inventory.transfer_batch",
    args = { item_refs = supplied },
  }, SESSION)
  ok(adapter ~= nil, "a dense array of valid refs resolves")
  same(args.item_refs, { listItemRef(1), listItemRef(2), listItemRef(3) },
    "every element passes through in order")
  notEqual(args.item_refs, supplied, "into a fresh table, never the caller's")
  supplied[1] = "mutated.after.resolve"
  equal(args.item_refs[1], listItemRef(1),
    "so a later mutation of the payload cannot reach the adapter")
end

Harness.group("a list that is not a dense array of fresh in-session refs is refused")
do
  local spy = Support.spyAdapter("inventory.transfer_batch", {
    args = {
      item_refs = {
        type = ARG.LIST, of = ARG.REF, required = true, max_items = 4, kinds = { item = true },
      },
    },
  })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "the list adapter registers")
  local r1, r2 = listItemRef(1), listItemRef(2)
  local sparse = {}
  sparse[1] = r1
  sparse[3] = r2
  local cases = {
    {
      name = "a scalar where a list belongs",
      value = "not-a-list",
      reason = REASON.INVALID_ARGUMENT,
      needle = "must be a list",
    },
    {
      name = "an empty list",
      value = {},
      reason = REASON.INVALID_ARGUMENT,
      needle = "at least one item",
    },
    {
      name = "a sparse table",
      value = sparse,
      reason = REASON.INVALID_ARGUMENT,
      needle = "dense array",
    },
    {
      name = "a keyed table",
      value = { first = r1 },
      reason = REASON.INVALID_ARGUMENT,
      needle = "dense array",
    },
    {
      name = "one item over the declared bound",
      value = { listItemRef(1), listItemRef(2), listItemRef(3), listItemRef(4), listItemRef(5) },
      reason = REASON.INVALID_ARGUMENT,
      needle = "more than 4 items",
    },
    {
      name = "a duplicate element",
      value = { r1, r2, r1 },
      reason = REASON.INVALID_ARGUMENT,
      needle = "item 3 repeats item 1",
    },
    {
      name = "a Java class name as an element",
      value = { r1, "zombie.inventory.ItemContainer" },
      reason = REASON.INVALID_REF,
      needle = "item 2",
    },
    {
      name = "an element minted by another session",
      value = { r1, listItemRef(2, Support.OTHER_SESSION) },
      reason = REASON.INVALID_REF,
      needle = "item 2",
    },
    {
      name = "an element of the wrong ref kind",
      value = { r1, PZ.Refs.buildSquare(SESSION, 10, 20, 0) },
      reason = REASON.INVALID_REF,
      needle = "item 2",
    },
    {
      name = "a null element",
      value = { r1, PZ.Json.null },
      reason = REASON.INVALID_REF,
      needle = "item 2",
    },
    {
      name = "a nested table element",
      value = { r1, { r2 } },
      reason = REASON.INVALID_REF,
      needle = "item 2",
    },
  }
  for index = 1, #cases do
    local case = cases[index]
    local adapter, reasonCode, detail = registry:resolve({
      action = "inventory.transfer_batch",
      args = { item_refs = case.value },
    }, SESSION)
    isNil(adapter, case.name .. " is refused")
    equal(reasonCode, case.reason, case.name .. " carries the right reason")
    contains(detail, case.needle, "and the detail names the problem")
  end
  equal(spy.starts, 0, "the adapter was never called")
  isNil(spy.last_args, "and never saw an argument table")
end

Harness.group("a table for a non-list declaration is still refused as a scalar, verbatim")
do
  local spy = Support.spyAdapter("inventory.transfer", {
    args = { item = { type = ARG.REF, required = true, kinds = { item = true } } },
  })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "the scalar adapter registers")
  local adapter, reasonCode, detail = registry:resolve({
    action = "inventory.transfer",
    args = { item = { itemRef() } },
  }, SESSION)
  isNil(adapter, "a table where a ref belongs is refused")
  equal(reasonCode, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  equal(detail, 'argument "item" must be a scalar',
    "and the scalar refusal is word for word what it always was")
  equal(spy.starts, 0, "the adapter was never called")
end

Harness.group("a list of strings runs each element through the token checks")
do
  local spy = Support.spyAdapter("world.inspect", {
    args = { notes = { type = ARG.LIST, of = ARG.STRING, max_items = 3, max_bytes = 8 } },
  })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "a string list registers with a byte bound and no kinds")
  local adapter, args = registry:resolve({
    action = "world.inspect",
    args = { notes = { "kitchen", "shed-2" } },
  }, SESSION)
  ok(adapter ~= nil, "plain tokens pass")
  same(args.notes, { "kitchen", "shed-2" }, "in order")

  local refused = {
    { value = { "with space" }, needle = "item 1" },
    { value = { "kitchen", "123456789" }, needle = "item 2 must be 1..8 bytes" },
    { value = { "" }, needle = "item 1 must be 1..8 bytes" },
    { value = { 42 }, needle = "item 1 must be a string" },
    { value = { "twice", "twice" }, needle = "item 2 repeats item 1" },
  }
  for index = 1, #refused do
    local case = refused[index]
    local resolved, reasonCode, detail = registry:resolve({
      action = "world.inspect",
      args = { notes = case.value },
    }, SESSION)
    isNil(resolved, string.format("string list %d is refused", index))
    equal(reasonCode, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
    contains(detail, case.needle, "and the detail names the element")
  end
end

Harness.group("a list counts as one argument against MAX_ARGS")
do
  local declaration = {
    items = { type = ARG.LIST, of = ARG.STRING, max_items = Dispatcher.MAX_LIST_ITEMS },
  }
  for index = 1, Dispatcher.MAX_ARGS - 1 do
    declaration["k" .. index] = { type = ARG.STRING }
  end
  local spy = Support.spyAdapter("world.inspect", { args = declaration })
  local registry = Dispatcher.new()
  ok(registry:register(spy), "eight declarations, one a full-width list, register")
  local payload = { items = { "a", "b", "c", "d", "e", "f", "g", "h" } }
  for index = 1, Dispatcher.MAX_ARGS - 1 do
    payload["k" .. index] = "v" .. index
  end
  local adapter, args = registry:resolve({ action = "world.inspect", args = payload }, SESSION)
  ok(adapter ~= nil, "eight arguments carrying eight list items resolve")
  equal(#args.items, Dispatcher.MAX_LIST_ITEMS, "with every item present")
end

Harness.finish("test_command_dispatcher")
