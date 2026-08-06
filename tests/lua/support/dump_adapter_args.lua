--[[
Print every shipped adapter's argument declaration as JSON, for the Python side
to check its own payloads against.

The mod and the sidecar agree about arguments only by convention: the sidecar's
`build_args` produces a table, the mod's PZAgent.CommandDispatcher rebuilds one
from the adapter's declaration, and nothing compared the two. A key the sidecar
emits and the adapter never declared is not ignored -- the command is refused --
so the disagreement costs every command of that action, and no test on either
side of the boundary can see it.

Run from the repository root:

    lua5.4 tests/lua/support/dump_adapter_args.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")

local PZ = Harness.loadModules()
dofile("pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")

--- Listed rather than globbed, for the same reason the registry test lists them:
--- a file that stops loading must break this, not quietly shrink the output.
Support.loadModules("", {
  "Movement",
  "World",
  "Containers",
  "Inventory",
  "Consumption",
  "Literature",
  "Equipment",
  "Medical",
  "Rest",
  "Sleep",
})

local document = {}
for _, adapter in ipairs(PZ.Adapters.ALL) do
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
    if spec.integer == true then
      entry.integer = true
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
  document[adapter.action] = declared
end

local encoded = PZ.Json.encode(document)
print(encoded)
