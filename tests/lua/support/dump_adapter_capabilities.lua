--[[
Print the capability each shipped adapter declares, as JSON.

Separate from dump_adapter_args.lua rather than folded into it: that file's
output shape is what tests/contract/test_adapter_args_agreement.py parses, and
widening it to carry a second unrelated fact would make one failure look like
the other.

The two sides of the wire mean different things by this field. The sidecar gates
an action on `adapter.required_capability`; the mod publishes its capability
document keyed on `adapter.capability`, which is what a person reads to find out
why something was refused. Nothing compared them, and five adapters had drifted
to `nil` with comments claiming no probe existed for them.

Run from the repository root:

    lua5.4 tests/lua/support/dump_adapter_capabilities.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")

local PZ = Harness.loadModules()
dofile("pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")

--- Listed rather than globbed, for the same reason the registry test lists
--- them: a file that stops loading must break this, not quietly shrink the
--- output.
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
  "Combat",
  "Crafting",
  "Building",
})

local document = {}
for _, adapter in ipairs(PZ.Adapters.ALL) do
  -- `false` rather than nil: a nil value would drop the key from the JSON
  -- object entirely, and "this adapter declares no capability" would become
  -- indistinguishable from "this adapter did not load".
  document[adapter.action] = adapter.capability or false
end

local encoded = PZ.Json.encode(document)
print(encoded)
