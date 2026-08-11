--[[
PZAgent.CommandDispatcher -- the closed gate between a decoded command and the
adapter that would run it.

Invariant: an adapter is reached only for an action name that is in
PZAgent.Protocol.ACTIONS, with an argument table whose every key was declared by
that adapter and whose every value passed the check its declaration demands.
There is no other route in. Two consequences are worth stating plainly because
they are the reason this file exists as a separate layer:

**A command names an action, never code.** The action is looked up in a fixed
table, exactly as PZAgent.Ipc looks a file role up in a fixed table. A string
that is not a key is an error, so there is no path from a decoded command to a
function name, a global, a Java class, a file path or a chunk of Lua. Nothing
here compiles a string, indexes _G with command data, or concatenates command
data into anything that is later interpreted.

**An undeclared argument is a rejection, not an ignored field.** Dropping it
silently would make the difference between "the adapter refused this" and "the
adapter never saw this" invisible on the wire, and it would let a payload
designed for a future adapter sit in the args table until one appears. The
sanitised table handed to an adapter is rebuilt from the declaration, so even a
loosened check cannot leak an extra key through.

Registration is single-assignment. Silently replacing an adapter would let load
order decide which code owns an action, and that is the last thing that should
move quietly.

This file decides nothing about safety, arming or ordering: that is
PZAgent.Safety and PZAgent.ActionRuntime.
]]

PZAgent = PZAgent or {}

local CommandDispatcher = {}
PZAgent.CommandDispatcher = CommandDispatcher

--- Argument kinds an adapter may declare. The set is closed for the same reason
--- the action set is: a declaration is a contract the checker below has to
--- understand, not a hint.
CommandDispatcher.ARG = {
  STRING = "string",
  NUMBER = "number",
  BOOLEAN = "boolean",
  REF = "ref",
  ENUM = "enum",
  LIST = "list",
}

--- Arguments one command may carry. Every adapter in this project declares
--- fewer than a handful; the bound is here so a hostile payload cannot make the
--- validation loop long.
CommandDispatcher.MAX_ARGS = 8

--- Elements one declared list may carry. A list counts as one argument against
--- MAX_ARGS, so without its own bound a single list would be the unbounded
--- collection the args cap exists to prevent. A declaration asks for its own
--- smaller max_items; this is the ceiling no declaration may raise.
CommandDispatcher.MAX_LIST_ITEMS = 8

--- Bound on a declared string argument. Long enough for a mode name or an item
--- type, far too short for a script.
CommandDispatcher.MAX_STRING_BYTES = 64

--- Alphabet a plain string argument may use. Deliberately narrower than JSON's:
--- no quotes, no parentheses, no slashes, no backslashes, no whitespace, no
--- control characters. A value that could be read as a path, a call or a Lua
--- fragment does not match, and is refused before an adapter sees it.
local STRING_PATTERN = "^[A-Za-z0-9_%.%-]+$"

--- Engine symbol names an adapter may declare as required. `Class.member` is
--- allowed one level deep, which is what PZAgent.CapabilityRuntime probes.
local SYMBOL_PATTERN = "^[A-Za-z_][A-Za-z0-9_]*%.?[A-Za-z0-9_]*$"

local Registry = {}
Registry.__index = Registry

--- Create an empty registry.
function CommandDispatcher.new()
  return setmetatable({ adapters = {}, names = {} }, Registry)
end

local function checkArgSpec(action, name, spec)
  if type(name) ~= "string" or name:match(STRING_PATTERN) == nil then
    return nil, string.format("%s declares an argument name that is not a plain token", action)
  end
  if type(spec) ~= "table" then
    return nil, string.format("%s: argument %q has no declaration", action, name)
  end
  local kind = spec.type
  local known = false
  for _, value in pairs(CommandDispatcher.ARG) do
    if value == kind then
      known = true
    end
  end
  if not known then
    return nil, string.format("%s: argument %q declares unknown type %s", action, name, tostring(kind))
  end
  -- Emptiness via PZAgent.Compat, not the global `next`: the 2026-08-08 live
  -- run proved Kahlua does not provide it, and hasEntries answers false for a
  -- non-table, which keeps the "not a table" branch these checks had.
  if kind == CommandDispatcher.ARG.ENUM then
    if not PZAgent.Compat.hasEntries(spec.values) then
      return nil, string.format("%s: enum argument %q declares no values", action, name)
    end
  end
  if kind == CommandDispatcher.ARG.REF then
    if not PZAgent.Compat.hasEntries(spec.kinds) then
      return nil, string.format("%s: ref argument %q declares no accepted ref kinds", action, name)
    end
  end
  if kind == CommandDispatcher.ARG.LIST then
    -- Elements are limited to refs and plain strings on purpose: every element
    -- kind a list may carry is a value the checker below hands to game code N
    -- times per command, so each new kind is new attack surface on the game
    -- thread. Numbers, booleans, enums and above all nested lists stay refused
    -- until an adapter actually needs one, and earns it a checker of its own.
    local of = spec.of
    if of ~= CommandDispatcher.ARG.REF and of ~= CommandDispatcher.ARG.STRING then
      return nil, string.format(
        "%s: list argument %q must declare elements of ref or string, not %s",
        action, name, tostring(of))
    end
    local maxItems = spec.max_items
    if type(maxItems) ~= "number" or maxItems ~= math.floor(maxItems)
      or maxItems < 1 or maxItems > CommandDispatcher.MAX_LIST_ITEMS then
      return nil, string.format(
        "%s: list argument %q must declare max_items in 1..%d",
        action, name, CommandDispatcher.MAX_LIST_ITEMS)
    end
    if of == CommandDispatcher.ARG.REF and not PZAgent.Compat.hasEntries(spec.kinds) then
      return nil, string.format(
        "%s: ref-list argument %q declares no accepted ref kinds", action, name)
    end
  end
  -- A bound is only ever used in a comparison against the value that arrived
  -- (`#value > max_bytes`, `value < min`). One that is not a number does not
  -- refuse that value: it raises, inside PZAgent.ActionRuntime's admission
  -- path, on every command this action ever receives. That is a malformed
  -- declaration -- a bug in the mod -- so it is caught here at load, which is
  -- what the rest of this function is for.
  local bounds = { "min", "max", "max_bytes" }
  for index = 1, #bounds do
    local field = bounds[index]
    if spec[field] ~= nil and type(spec[field]) ~= "number" then
      return nil, string.format(
        "%s: argument %q declares %s as %s, which cannot be compared with a value",
        action,
        name,
        field,
        type(spec[field])
      )
    end
  end
  return true
end

--- Register one adapter. Returns true, or nil plus a reason.
---
--- Everything an adapter claims is checked here rather than at dispatch time: a
--- malformed declaration is a bug in the mod, and finding it at load is the
--- difference between a mod that refuses to arm and a mod that fails on the
--- first command of a session.
function Registry:register(adapter)
  if type(adapter) ~= "table" then
    return nil, "an adapter must be a table"
  end
  local action = adapter.action
  if not PZAgent.Protocol.isKnownAction(action) then
    return nil, string.format("%s is not an action in the protocol whitelist", tostring(action))
  end
  if self.adapters[action] ~= nil then
    return nil, string.format("an adapter for %s is already registered", action)
  end
  if type(adapter.start) ~= "function" then
    return nil, string.format("%s: an adapter must provide start()", action)
  end
  if adapter.poll ~= nil and type(adapter.poll) ~= "function" then
    return nil, string.format("%s: poll must be a function when it is provided", action)
  end
  if adapter.interrupt ~= nil and type(adapter.interrupt) ~= "function" then
    return nil, string.format("%s: interrupt must be a function when it is provided", action)
  end
  local symbols = adapter.required_symbols or {}
  if type(symbols) ~= "table" then
    return nil, string.format("%s: required_symbols must be a table", action)
  end
  for index = 1, #symbols do
    local symbol = symbols[index]
    if type(symbol) ~= "string" or symbol:match(SYMBOL_PATTERN) == nil then
      return nil, string.format("%s: %s is not a plain engine symbol name", action, tostring(symbol))
    end
  end
  if adapter.capability ~= nil then
    if type(adapter.capability) ~= "string" or adapter.capability:match(STRING_PATTERN) == nil then
      return nil, string.format("%s: capability must be a plain token", action)
    end
  end
  local args = adapter.args or {}
  if type(args) ~= "table" then
    return nil, string.format("%s: args must be a table of declarations", action)
  end
  local declared = 0
  for name, spec in pairs(args) do
    declared = declared + 1
    local ok, reason = checkArgSpec(action, name, spec)
    if ok == nil then
      return nil, reason
    end
  end
  if declared > CommandDispatcher.MAX_ARGS then
    return nil, string.format("%s declares more than %d arguments", action, CommandDispatcher.MAX_ARGS)
  end
  self.adapters[action] = adapter
  self.names[#self.names + 1] = action
  return true
end

--- The adapter registered for `action`, or nil.
function Registry:adapterFor(action)
  if not PZAgent.Protocol.isKnownAction(action) then
    return nil
  end
  return self.adapters[action]
end

--- Every registered action name, in registration order.
function Registry:registered()
  local out = {}
  for index = 1, #self.names do
    out[index] = self.names[index]
  end
  return out
end

-- ---------------------------------------------------------------------------
-- argument validation
-- ---------------------------------------------------------------------------

local function checkString(spec, value)
  if type(value) ~= "string" then
    return nil, "must be a string"
  end
  local limit = spec.max_bytes or CommandDispatcher.MAX_STRING_BYTES
  if #value == 0 or #value > limit then
    return nil, string.format("must be 1..%d bytes", limit)
  end
  if value:match(STRING_PATTERN) == nil then
    return nil, "contains characters a plain token may not carry"
  end
  return value
end

local function checkNumber(spec, value)
  if type(value) ~= "number" then
    return nil, "must be a number"
  end
  -- NaN and the infinities: neither compares usefully against a bound, and both
  -- reach here only from a hand-written document, since PZAgent.Json refuses to
  -- encode them.
  if value ~= value or value == math.huge or value == -math.huge then
    return nil, "must be a finite number"
  end
  if spec.integer and value ~= math.floor(value) then
    return nil, "must be an integer"
  end
  if spec.min ~= nil and value < spec.min then
    return nil, string.format("must be at least %s", tostring(spec.min))
  end
  if spec.max ~= nil and value > spec.max then
    return nil, string.format("must be at most %s", tostring(spec.max))
  end
  return value
end

local function checkRef(spec, value, sessionId)
  if type(value) ~= "string" then
    return nil, "must be a reference string", PZAgent.Protocol.REASON.INVALID_REF
  end
  local parsed, parseError = PZAgent.Refs.parse(value)
  if parsed == nil then
    return nil, parseError, PZAgent.Protocol.REASON.INVALID_REF
  end
  if spec.kinds[parsed.kind] ~= true then
    return nil,
      string.format("a %s reference is not accepted here", tostring(parsed.kind)),
      PZAgent.Protocol.REASON.INVALID_REF
  end
  if not PZAgent.Refs.belongsToSession(value, sessionId) then
    -- A reference from another session is not stale, it is wrong: its runtime
    -- ids denote different objects now, so this is never a reason to retry.
    return nil, "reference was not minted by the open session", PZAgent.Protocol.REASON.INVALID_REF
  end
  return value
end

--- Validate one list argument: a dense array whose every element passes the
--- same checker that element kind gets when it stands alone -- checkRef with
--- the session, checkString with the declaration's byte bound.
---
--- Returns a fresh sanitised array, never the caller's table: the payload
--- still holds a reference to what it sent, and an adapter must not share
--- state with it. Or nil, detail, reasonCode -- the element checker's code is
--- propagated, so a bad ref element is INVALID_REF exactly as a bad lone ref.
local function checkList(spec, value, sessionId)
  if type(value) ~= "table" then
    return nil, "must be a list"
  end
  -- Dense-array walk, bounded before the table is trusted: count entries with
  -- pairs and refuse the moment the count passes the declared bound, so a
  -- hostile table cannot make this loop long. Then demand keys exactly 1..n.
  -- PZAgent.Json decodes a JSON array into precisely that shape, so a sparse
  -- or keyed table never came off the wire -- it is refused, not repaired.
  local count = 0
  for _ in pairs(value) do
    count = count + 1
    if count > spec.max_items then
      return nil, string.format("carries more than %d items", spec.max_items)
    end
  end
  if count == 0 then
    return nil, "must carry at least one item"
  end
  for index = 1, count do
    if value[index] == nil then
      return nil, "must be a dense array"
    end
  end
  local sanitised = {}
  local seen = {}
  for index = 1, count do
    local element = value[index]
    local checked, detail, reasonCode
    if spec.of == CommandDispatcher.ARG.STRING then
      checked, detail = checkString(spec, element)
    elseif spec.of == CommandDispatcher.ARG.REF then
      checked, detail, reasonCode = checkRef(spec, element, sessionId)
    else
      -- Unreachable through register(), which refuses any other element kind
      -- at load; kept explicit so a hand-built declaration fails here instead
      -- of quietly becoming whichever check an else happened to name.
      return nil, string.format("item %d has an element kind this dispatcher does not know", index)
    end
    if checked == nil then
      return nil, string.format("item %d %s", index, tostring(detail)), reasonCode
    end
    -- A duplicate element is one object asked to move twice: the second copy
    -- could only succeed by lying about the first, so the whole list is wrong.
    if seen[checked] ~= nil then
      return nil, string.format("item %d repeats item %d", index, seen[checked])
    end
    seen[checked] = index
    sanitised[index] = checked
  end
  return sanitised
end

--- Validate `args` against `adapter`'s declaration.
---
--- Returns the sanitised table, or nil plus a reason code and detail. The
--- returned table is built from the declaration, so it holds declared keys and
--- nothing else even if a check above were to be loosened.
function CommandDispatcher.checkArgs(adapter, args, sessionId)
  local reasons = PZAgent.Protocol.REASON
  local declaration = adapter.args or {}
  if args == nil then
    args = {}
  end
  if type(args) ~= "table" then
    return nil, reasons.INVALID_ARGUMENT, "args must be an object"
  end
  local count = 0
  for name, value in pairs(args) do
    count = count + 1
    if count > CommandDispatcher.MAX_ARGS then
      return nil, reasons.INVALID_ARGUMENT,
        string.format("more than %d arguments were supplied", CommandDispatcher.MAX_ARGS)
    end
    if type(name) ~= "string" or declaration[name] == nil then
      return nil, reasons.INVALID_ARGUMENT,
        string.format("%s does not accept an argument named %s", adapter.action, tostring(name))
    end
    if value == PZAgent.Json.null then
      return nil, reasons.INVALID_ARGUMENT, string.format("argument %q is null", name)
    end
    if type(value) == "table" and declaration[name].type ~= CommandDispatcher.ARG.LIST then
      -- Every declared kind except LIST is a scalar, so a table is either a
      -- structure no adapter asked for or a nesting depth nobody bounded. A
      -- table under a LIST declaration passes here and faces checkList below,
      -- which bounds its length before trusting it.
      return nil, reasons.INVALID_ARGUMENT,
        string.format("argument %q must be a scalar", name)
    end
  end

  local sanitised = {}
  for name, spec in pairs(declaration) do
    local value = args[name]
    if value == nil then
      if spec.required then
        return nil, reasons.INVALID_ARGUMENT, string.format("argument %q is required", name)
      end
      if spec.default ~= nil then
        sanitised[name] = spec.default
      end
    else
      local checked, detail, reasonCode
      if spec.type == CommandDispatcher.ARG.STRING then
        checked, detail = checkString(spec, value)
      elseif spec.type == CommandDispatcher.ARG.NUMBER then
        checked, detail = checkNumber(spec, value)
      elseif spec.type == CommandDispatcher.ARG.BOOLEAN then
        if type(value) == "boolean" then
          checked = value
        else
          detail = "must be a boolean"
        end
      elseif spec.type == CommandDispatcher.ARG.ENUM then
        if type(value) == "string" and spec.values[value] == true then
          checked = value
        else
          detail = "is not one of the accepted values"
        end
      elseif spec.type == CommandDispatcher.ARG.LIST then
        checked, detail, reasonCode = checkList(spec, value, sessionId)
      elseif spec.type == CommandDispatcher.ARG.REF then
        checked, detail, reasonCode = checkRef(spec, value, sessionId)
      else
        -- Unreachable through register(), which refuses an unknown declared
        -- type at load. Kept because checkArgs takes any adapter table, and
        -- this branch used to be a bare else falling into checkRef -- the
        -- shape under which a typo'd declaration silently became a ref check
        -- instead of a refusal.
        return nil, reasons.INVALID_ARGUMENT,
          string.format("argument %q declares a type this dispatcher does not know", name)
      end
      if checked == nil then
        return nil, reasonCode or reasons.INVALID_ARGUMENT,
          string.format("argument %q %s", name, tostring(detail))
      end
      sanitised[name] = checked
    end
  end
  return sanitised
end

--- Resolve a command to its adapter and sanitised arguments.
---
--- Returns adapter, args -- or nil plus a reason code and detail. An action
--- with no adapter is CAPABILITY_UNAVAILABLE naming the action: from the
--- sidecar's side an unimplemented action and one whose game API is missing are
--- the same permanent fact about this build.
function Registry:resolve(command, sessionId)
  local reasons = PZAgent.Protocol.REASON
  if type(command) ~= "table" then
    return nil, reasons.INVALID_ARGUMENT, "a command must be an object"
  end
  local action = command.action
  if not PZAgent.Protocol.isKnownAction(action) then
    return nil, reasons.INVALID_ARGUMENT,
      string.format("action %s is not in the protocol whitelist", tostring(action))
  end
  local adapter = self.adapters[action]
  if adapter == nil then
    return nil, reasons.CAPABILITY_UNAVAILABLE,
      string.format("this build has no adapter for %s", action)
  end
  local args, reasonCode, detail = CommandDispatcher.checkArgs(adapter, command.args, sessionId)
  if args == nil then
    return nil, reasonCode, detail
  end
  return adapter, args
end

return CommandDispatcher
