--[[
Print the protocol vocabularies the mod actually resolves, as JSON.

`tests/unit/test_lua_mod_contract.py` already holds most of Protocol.lua against
the Python enums, and it does so by matching `KEY = "value"` pairs in the file's
text. That works for the tables written as literals and cannot work for the ones
that are not: `TERMINAL_STATUSES`, `MUTATING_MODES`, `ACTIONS` and `DANGER_RANK`
are built by `toSet(...)` and by keying off other tables, so a pattern looking
for quoted pairs sees nothing in them. They were therefore left unchecked, and
three of the four carry decisions that matter on both sides of the wire:

- `TERMINAL_STATUSES` decides when the mod stops tracking an action. NEVER
  TERMINAL is one of the three defect families this project names. A status the
  sidecar treats as finished and the mod does not is an action the mod keeps
  alive forever, and the two sets are maintained in different languages by
  different edits.
- `MUTATING_MODES` decides which session modes accept a command that changes the
  world at all. A mode that mutates on one side and not the other is a safety
  question, not a formatting one.
- `DANGER_RANK` is the order the reflex guard compares against, so a rank that
  disagrees is a threshold that fires at the wrong level.

Running the producer is also the answer to the failure this repository has
already had once: a `kind = "square"` producer written through a constant was
invisible to a regex, and two documents were retracted on the strength of what
the regex could not see. The other three seams -- adapter args, adapter
capabilities, the observation document -- all dump what the mod resolves rather
than what its source looks like. This is the fourth.

Nothing here builds a vocabulary of its own. Every value printed is read off the
loaded `PZAgent.Protocol` table, so what comes out is what the mod would use.

Run from the repository root:

    lua5.4 tests/lua/support/dump_protocol_tables.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")

local PZ = Harness.loadModules()
local Protocol = PZ.Protocol

--- The keys of a set-shaped table, as a sorted array.
---
--- `toSet` produces `{ [value] = true }`, so the vocabulary is in the keys.
--- Sorted so the JSON is stable and a diff is about content rather than about
--- whichever order the Lua hash part happened to hand back.
local function setKeys(table_)
  local keys = {}
  for key, present in pairs(table_) do
    if present then
      keys[#keys + 1] = key
    end
  end
  table.sort(keys)
  return keys
end

--- A plain `KEY = "value"` table, as it stands after loading.
local function stringMap(table_)
  local copy = {}
  for key, value in pairs(table_) do
    copy[key] = value
  end
  return copy
end

--- A table keyed by a protocol value rather than by a name, e.g. DANGER_RANK.
local function valueMap(table_)
  local copy = {}
  for key, value in pairs(table_) do
    copy[tostring(key)] = value
  end
  return copy
end

local document = {
  versions = {
    protocol = Protocol.PROTOCOL_VERSION,
    schema = Protocol.SCHEMA_VERSION,
    mod = Protocol.MOD_VERSION,
    target_build = Protocol.TARGET_BUILD,
  },
  supported_builds = Protocol.SUPPORTED_BUILDS,
  action_names = Protocol.ACTION_NAMES,
  actions = setKeys(Protocol.ACTIONS),
  read_only_actions = setKeys(Protocol.READ_ONLY_ACTIONS),
  always_allowed_actions = setKeys(Protocol.ALWAYS_ALLOWED_ACTIONS),
  terminal_statuses = setKeys(Protocol.TERMINAL_STATUSES),
  mutating_modes = setKeys(Protocol.MUTATING_MODES),
  danger_rank = valueMap(Protocol.DANGER_RANK),
  status = stringMap(Protocol.STATUS),
  mode = stringMap(Protocol.MODE),
  danger = stringMap(Protocol.DANGER),
  ownership = stringMap(Protocol.OWNERSHIP),
  capability = stringMap(Protocol.CAPABILITY),
  peer = stringMap(Protocol.PEER),
  reason = stringMap(Protocol.REASON),
}

print((PZ.Json.encode(document)))
