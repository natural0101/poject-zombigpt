--[[
Print every shipped adapter's argument declaration as JSON, for the Python side
to check its own payloads against.

The mod and the sidecar agree about arguments only by convention: the sidecar's
`build_args` produces a table, the mod's PZAgent.CommandDispatcher rebuilds one
from the adapter's declaration, and nothing compared the two. A key the sidecar
emits and the adapter never declared is not ignored -- the command is refused --
so the disagreement costs every command of that action, and no test on either
side of the boundary can see it.

Both adapter families are dumped: the game-facing files that publish into
PZAgent.Adapters.ALL, and ActionRuntime's own control adapters -- which is
where `action.wait` and `plan.cancel` actually live. Walking only the published
list is how those two escaped the agreement check while their arguments
disagreed with the sidecar's.

Run from the repository root:

    lua5.4 tests/lua/support/dump_adapter_args.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")

local PZ = Harness.loadModules()
dofile("pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
dofile("pz-mod/42/media/lua/client/PZAgent/ActionRuntime.lua")

--- Listed rather than globbed, for the same reason the registry test lists them:
--- a file that stops loading must break this, not quietly shrink the output.
Support.loadModules("", {
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
})

--- One adapter's argument declaration, as plain JSON-encodable values.
local function declarationOf(adapter)
  local declared = {}
  for name, spec in pairs(adapter.args or {}) do
    local entry = {
      type = spec.type,
      required = spec.required == true,
    }
    if spec.min ~= nil then
      entry.min = spec.min
    end
    if spec.max ~= nil then
      entry.max = spec.max
    end
    if spec.max_bytes ~= nil then
      entry.max_bytes = spec.max_bytes
    end
    if spec.integer == true then
      entry.integer = true
    end
    -- A list declaration carries its element kind and its own length bound;
    -- `kinds` (when of == "ref") and `max_bytes` (when of == "string") ride
    -- the generic branches above, so a list dumps as
    -- {type:"list", of:"ref", max_items:8, kinds:[...]}.
    if spec.of ~= nil then
      entry.of = spec.of
    end
    if spec.max_items ~= nil then
      entry.max_items = spec.max_items
    end
    if spec.values ~= nil then
      local values = {}
      for value in pairs(spec.values) do
        values[#values + 1] = value
      end
      table.sort(values)
      entry.values = values
    end
    if spec.kinds ~= nil then
      local kinds = {}
      for kind in pairs(spec.kinds) do
        kinds[#kinds + 1] = kind
      end
      table.sort(kinds)
      entry.kinds = kinds
    end
    declared[name] = entry
  end
  return declared
end

-- The same precedence ActionRuntime.install applies, read from the same table:
-- a published adapter wins any action that touches the world, and the four
-- runtime-owned control actions are always served by the built-ins. What this
-- dump reports must be the declaration that would actually gate a command.
local claimed = {}
for _, adapter in ipairs(PZ.Adapters.ALL) do
  claimed[adapter.action] = true
end

local document = {}
for _, adapter in ipairs(PZ.ActionRuntime.controlAdapters()) do
  if PZ.ActionRuntime.RUNTIME_OWNED[adapter.action] or not claimed[adapter.action] then
    document[adapter.action] = declarationOf(adapter)
  end
end
for _, adapter in ipairs(PZ.Adapters.ALL) do
  if document[adapter.action] == nil then
    document[adapter.action] = declarationOf(adapter)
  end
end

local encoded = PZ.Json.encode(document)
print(encoded)
