--[[
Minimal test harness for the mod's Lua modules.

CI runs `lua5.4 tests/lua/<file>.lua` directly -- no busted, no luarocks -- so
this file provides the two things such a script needs: a way to load the mod's
modules out of the repository, and assertions that report every failure instead
of aborting on the first one.

The modules are loaded with dofile rather than require: inside the game they are
loaded the same way, by the engine walking media/lua and executing each file
into one shared global namespace, so loading them this way exercises the same
contract -- each file must populate PZAgent.* and must not depend on being
required.
]]

local Harness = {}

--- Repository root, derived from the path this test was invoked with, so the
--- suite works from any working directory.
local function repositoryRoot()
  local invoked = arg and arg[0] or "tests/lua/unknown.lua"
  local prefix = invoked:match("^(.*)tests[/\\]lua[/\\]")
  return prefix or ""
end

Harness.root = repositoryRoot()

local MOD_LUA = "pz-mod/42/media/lua/"

--- Load order mirrors the game's: shared before client.
local MODULES = {
  "shared/PZAgent/Json.lua",
  "shared/PZAgent/Protocol.lua",
  "shared/PZAgent/Refs.lua",
  "shared/PZAgent/Sequence.lua",
  "shared/PZAgent/Ownership.lua",
  "client/PZAgent/Ipc.lua",
  "client/PZAgent/Session.lua",
  "client/PZAgent/Safety.lua",
  "client/PZAgent/Heartbeat.lua",
  "client/PZAgent/Hud.lua",
  "client/PZAgent/Runtime.lua",
}

--- Load every mod module into the global PZAgent namespace.
function Harness.loadModules()
  for index = 1, #MODULES do
    local path = Harness.root .. MOD_LUA .. MODULES[index]
    local chunk, err = loadfile(path)
    if chunk == nil then
      error("could not load " .. path .. ": " .. tostring(err), 0)
    end
    chunk()
  end
  return PZAgent
end

-- ---------------------------------------------------------------------------
-- assertions
-- ---------------------------------------------------------------------------

local state = { passed = 0, failed = 0, failures = {}, suite = "" }

local function record(ok, message)
  if ok then
    state.passed = state.passed + 1
  else
    state.failed = state.failed + 1
    state.failures[#state.failures + 1] = message
    print("  FAIL " .. message)
  end
  return ok
end

--- Render a value for a failure message, sorting table keys so the output is
--- stable between runs.
local function render(value, depth)
  depth = depth or 0
  if type(value) ~= "table" or depth > 3 then
    if type(value) == "string" then
      return string.format("%q", value)
    end
    return tostring(value)
  end
  local keys = {}
  for key in pairs(value) do
    keys[#keys + 1] = tostring(key)
  end
  table.sort(keys)
  local parts = {}
  for index = 1, #keys do
    local key = keys[index]
    local item = value[key] ~= nil and value[key] or value[tonumber(key)]
    parts[index] = key .. "=" .. render(item, depth + 1)
  end
  return "{" .. table.concat(parts, ", ") .. "}"
end

Harness.render = render

function Harness.ok(value, message)
  return record(value and true or false, string.format("%s (got %s)", message, render(value)))
end

function Harness.isNil(value, message)
  return record(value == nil, string.format("%s (expected nil, got %s)", message, render(value)))
end

function Harness.equal(actual, expected, message)
  return record(
    actual == expected,
    string.format("%s\n    expected: %s\n    actual:   %s", message, render(expected), render(actual))
  )
end

function Harness.notEqual(actual, unexpected, message)
  return record(actual ~= unexpected, string.format("%s (both were %s)", message, render(actual)))
end

local function deepEqual(left, right)
  if left == right then
    return true
  end
  if type(left) ~= "table" or type(right) ~= "table" then
    return false
  end
  for key, value in pairs(left) do
    if not deepEqual(value, right[key]) then
      return false
    end
  end
  for key in pairs(right) do
    if left[key] == nil then
      return false
    end
  end
  return true
end

Harness.deepEqual = deepEqual

function Harness.same(actual, expected, message)
  return record(
    deepEqual(actual, expected),
    string.format("%s\n    expected: %s\n    actual:   %s", message, render(expected), render(actual))
  )
end

--- Assert that `text` contains `needle`, for error-message checks that should
--- not be pinned to exact wording.
function Harness.contains(text, needle, message)
  local found = type(text) == "string" and text:find(needle, 1, true) ~= nil
  return record(found, string.format("%s (%s does not contain %q)", message, render(text), needle))
end

--- Name the group of assertions that follow.
function Harness.group(name)
  state.suite = name
  print("- " .. name)
end

--- Print the summary and exit non-zero when anything failed.
function Harness.finish(suiteName)
  print(string.format("%s: %d passed, %d failed", suiteName, state.passed, state.failed))
  if state.failed > 0 then
    os.exit(1)
  end
  os.exit(0)
end

return Harness
