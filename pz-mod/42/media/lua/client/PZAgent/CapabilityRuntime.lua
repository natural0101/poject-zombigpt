--[[
PZAgent.CapabilityRuntime -- what this build can actually do, decided inside the
running game.

The sidecar's own probes (pz_agent_core.capabilities.probes) read the game's Lua
files off disk. Their best possible verdict is `available_unverified`, because a
symbol sitting in a file is a reason to try an action and never a reason to
claim it works. This file is the other half, and the ordering between them is
the whole point:

1. The symbols an adapter declares are resolved **in the live VM**, through
   pcall-guarded lookups. Present here means more than present on disk -- the
   class actually loaded -- but it is still only `available_unverified`. A
   symbol that resolves says nothing about what calling it does.
2. `verified` is minted in exactly one place: `noteAck`, and only for a
   succeeded acknowledgement carrying postcondition evidence. That ack can only
   have come from PZAgent.ActionRuntime, which will not mint one without an
   observed before/after. So there is no path from "the symbol exists" to
   "verified" that does not pass through an action that actually happened.

A failed ack whose reason code is CAPABILITY_UNAVAILABLE goes the other way and
drops the entry to `unsupported`, naming the symbol: that is a live observation
too, and the more important one -- it stops the sidecar publishing a write tool
for an API this build does not have.

An action nobody registered an adapter for is `unsupported` with a reason, never
absent. The report is over the protocol's closed action set, so "the sidecar
asked about an action this build never mentioned" cannot happen.

Nothing here writes a stat, fakes an effect, or upgrades anything on the
strength of a document found on disk.
]]

PZAgent = PZAgent or {}

local CapabilityRuntime = {}
PZAgent.CapabilityRuntime = CapabilityRuntime

--- Mirrors pz_agent_core.capabilities.model.MAX_REASON_LEN, so a reason written
--- here survives the sidecar's own bounds check unchanged.
CapabilityRuntime.MAX_REASON_BYTES = 200

--- Mirrors the reason strings of pz_agent_core.capabilities.model.
CapabilityRuntime.REASON_SYMBOL_MISSING = "SYMBOL_MISSING"
CapabilityRuntime.REASON_NO_ADAPTER = "NO_ADAPTER"
CapabilityRuntime.REASON_PROBE_NOT_CONFIRMED = "PROBE_NOT_CONFIRMED"

--- Same shape PZAgent.CommandDispatcher accepts in a declaration: a global, or
--- one member of a global. Re-checked here because this is the module that
--- indexes _G, and it must not do so on anything it has not proved is a plain
--- symbol name.
local SYMBOL_PATTERN = "^[A-Za-z_][A-Za-z0-9_]*%.?[A-Za-z0-9_]*$"

local Handle = {}
Handle.__index = Handle

local function truncate(text)
  if #text <= CapabilityRuntime.MAX_REASON_BYTES then
    return text
  end
  return text:sub(1, CapabilityRuntime.MAX_REASON_BYTES)
end

--- Resolve one declared engine symbol in the live VM.
---
--- Returns true, or nil plus a detail naming the symbol. Both the lookup of the
--- global and the indexing of its member are inside pcall: Kahlua exposes Java
--- objects whose members may be absent and whose indexing can itself raise, so
--- a build that spells a method differently must cost this one capability
--- rather than the tick that probed it.
function CapabilityRuntime.probeSymbol(name)
  if type(name) ~= "string" or name:match(SYMBOL_PATTERN) == nil then
    return nil, string.format("%s is not a plain engine symbol name", tostring(name))
  end
  if type(_G) ~= "table" then
    -- Reported as its own reason rather than as a missing symbol: "this Lua
    -- environment has no global table" is a fact about the interpreter, and
    -- filing it under the symbol's name would blame the wrong thing.
    return nil, "the Lua environment does not expose a global table to probe"
  end
  local head, member = name:match("^([^%.]+)%.([^%.]+)$")
  if head == nil then
    head = name
  end
  local ok, value = pcall(function()
    return _G[head]
  end)
  if not ok or value == nil then
    return nil, string.format("%s is not available in this build", head)
  end
  if member == nil then
    return true
  end
  local read, field = pcall(function()
    return value[member]
  end)
  if not read then
    return nil, string.format("reading %s raised: %s", name, tostring(field))
  end
  if type(field) ~= "function" then
    -- A declared member is always something the adapter intends to call. A
    -- present-but-not-callable field is a different API wearing the same name,
    -- which is exactly the case a bare presence check would wave through.
    return nil, string.format("%s is not callable in this build", name)
  end
  return true
end

--- Create a capability runtime over `dispatcher`.
function CapabilityRuntime.new(dispatcher)
  if type(dispatcher) ~= "table" or type(dispatcher.adapterFor) ~= "function" then
    return nil, "a capability runtime needs an adapter registry"
  end
  return setmetatable({
    dispatcher = dispatcher,
    revision = 0,
    probed_at_ms = nil,
    entries = {},
  }, Handle)
end

local function entryFor(self, action)
  local entry = self.entries[action]
  if entry == nil then
    entry = {
      action = action,
      state = PZAgent.Protocol.CAPABILITY.UNSUPPORTED,
      reason = CapabilityRuntime.REASON_NO_ADAPTER,
      symbols = {},
    }
    self.entries[action] = entry
  end
  return entry
end

--- Probe every action in the protocol's closed set against this build.
---
--- Returns the number of actions whose state changed, so a caller can decide
--- whether the report is worth republishing.
function Handle:probe(nowMs)
  local Protocol = PZAgent.Protocol
  local states = Protocol.CAPABILITY
  local changed = 0
  for index = 1, #Protocol.ACTION_NAMES do
    local action = Protocol.ACTION_NAMES[index]
    local entry = entryFor(self, action)
    local previous = entry.state
    local adapter = self.dispatcher:adapterFor(action)
    if adapter == nil then
      entry.state = states.UNSUPPORTED
      entry.reason = CapabilityRuntime.REASON_NO_ADAPTER
      entry.detail = "no adapter is registered for this action in this build"
      entry.symbols = {}
      entry.capability = nil
    else
      local symbols = adapter.required_symbols or {}
      local missing = {}
      for position = 1, #symbols do
        local ok, detail = CapabilityRuntime.probeSymbol(symbols[position])
        if ok == nil then
          missing[#missing + 1] = detail
        end
      end
      entry.capability = adapter.capability
      entry.symbols = {}
      for position = 1, #symbols do
        entry.symbols[position] = symbols[position]
      end
      if #missing > 0 then
        entry.state = states.UNSUPPORTED
        entry.reason = CapabilityRuntime.REASON_SYMBOL_MISSING
        entry.detail = truncate(table.concat(missing, "; "))
        entry.verified_at_ms = nil
        entry.command_id = nil
      elseif entry.state ~= states.VERIFIED then
        -- The symbols resolved in the live VM. That is a reason to try the
        -- action and nothing more, so the state stops here until an ack proves
        -- otherwise.
        entry.state = adapter.experimental and states.EXPERIMENTAL or states.AVAILABLE_UNVERIFIED
        entry.reason = adapter.experimental and CapabilityRuntime.REASON_PROBE_NOT_CONFIRMED or nil
        entry.detail = nil
      end
    end
    if entry.state ~= previous then
      changed = changed + 1
    end
  end
  self.probed_at_ms = nowMs
  if changed > 0 then
    self.revision = self.revision + 1
  end
  return changed
end

--- The state and reason recorded for `action`.
function Handle:stateOf(action)
  local entry = self.entries[action]
  if entry == nil then
    return PZAgent.Protocol.CAPABILITY.UNSUPPORTED, CapabilityRuntime.REASON_NO_ADAPTER, nil
  end
  return entry.state, entry.reason, entry.detail
end

--- True when `action` may be attempted at all on this build.
function Handle:isUsable(action)
  local state = self:stateOf(action)
  local states = PZAgent.Protocol.CAPABILITY
  return state == states.VERIFIED
    or state == states.AVAILABLE_UNVERIFIED
    or state == states.EXPERIMENTAL
end

--- Apply one terminal acknowledgement to the report.
---
--- This is the only function in the mod that may write `verified`, and it does
--- so only for a succeeded ack carrying evidence -- which PZAgent.ActionRuntime
--- will not produce without an observed postcondition. Returns true when the
--- report changed.
function Handle:noteAck(ack)
  if type(ack) ~= "table" or type(ack.action) ~= "string" then
    return false, "an ack must name its action"
  end
  local entry = self.entries[ack.action]
  if entry == nil then
    return false, string.format("%s is not an action this report covers", ack.action)
  end
  local Protocol = PZAgent.Protocol
  local states = Protocol.CAPABILITY

  if ack.status == Protocol.STATUS.SUCCEEDED then
    -- Emptiness via PZAgent.Compat, not the global `next`, which the 2026-08-08
    -- live run proved Kahlua does not provide; hasEntries also answers false
    -- for a non-table, preserving the type check this line used to spell out.
    if not PZAgent.Compat.hasEntries(ack.evidence) then
      return false, "a succeeded ack without evidence proves nothing"
    end
    if entry.state == states.UNSUPPORTED or entry.state == states.DISABLED_BY_POLICY then
      -- Unreachable through ActionRuntime, which refuses to start an action in
      -- either state. Reported rather than silently upgraded: if it ever
      -- happens, the gate has a hole and the report must not hide it.
      return false, string.format("%s is %s; a succeeded ack cannot raise it", ack.action, entry.state)
    end
    local changed = entry.state ~= states.VERIFIED
    entry.state = states.VERIFIED
    entry.reason = nil
    entry.detail = nil
    entry.verified_at_ms = ack.timestamp_ms
    entry.command_id = ack.command_id
    if changed then
      self.revision = self.revision + 1
    end
    return changed
  end

  if ack.reason_code == Protocol.REASON.CAPABILITY_UNAVAILABLE then
    if entry.reason == CapabilityRuntime.REASON_NO_ADAPTER then
      -- The refusal came from this report in the first place: an action nobody
      -- implemented answers CAPABILITY_UNAVAILABLE, and reading that back as
      -- "a symbol is missing" would replace the true reason with a guess.
      return false
    end
    local changed = entry.state ~= states.UNSUPPORTED
    entry.state = states.UNSUPPORTED
    entry.reason = CapabilityRuntime.REASON_SYMBOL_MISSING
    entry.detail = truncate(tostring(ack.message or "the build refused the API this action needs"))
    entry.verified_at_ms = nil
    entry.command_id = nil
    if changed then
      self.revision = self.revision + 1
    end
    return changed
  end

  -- Every other failure is about the world, not about the API: no food in the
  -- bag does not mean eating is unsupported, so the report is left alone.
  return false
end

--- The document written to capabilities.json.
---
--- Two views of the same probe results. `capabilities` is keyed by the probe
--- names of blueprint 3.8, which is what the sidecar's report model speaks;
--- `actions` is keyed by protocol action, which is what a command names. An
--- action whose adapter declares no probe name appears only in `actions`.
function Handle:report(nowMs)
  local Protocol = PZAgent.Protocol
  local document = {
    build = Protocol.TARGET_BUILD,
    protocol_version = Protocol.PROTOCOL_VERSION,
    schema_version = Protocol.SCHEMA_VERSION,
    mod_version = Protocol.MOD_VERSION,
    source = "mod_runtime_probe",
    revision = self.revision,
    generated_at_ms = nowMs,
    probed_at_ms = self.probed_at_ms,
    capabilities = {},
    actions = {},
  }
  local build = PZAgent.Heartbeat.detectBuild()
  if build ~= nil then
    document.build = build
    document.build_verified = true
    document.build_supported = Protocol.isSupportedBuild(build)
  else
    -- The build could not be read, so the target build is reported as the
    -- assumption it is rather than as an observation.
    document.build_verified = false
    document.build_supported = false
  end
  for index = 1, #Protocol.ACTION_NAMES do
    local action = Protocol.ACTION_NAMES[index]
    local entry = self.entries[action]
    if entry ~= nil then
      local view = {
        state = entry.state,
        reason = entry.reason,
        detail = entry.detail,
        capability = entry.capability,
        action = action,
        verified_at_ms = entry.verified_at_ms,
        command_id = entry.command_id,
      }
      if #entry.symbols > 0 then
        view.symbols = entry.symbols
      end
      document.actions[action] = view
      if entry.capability ~= nil then
        document.capabilities[entry.capability] = view
      end
    end
  end
  return document
end

--- Write the report to capabilities.json. Returns the document, or nil plus a
--- reason -- a report that could not be written must not be reported as
--- published.
function Handle:publish(ipc, nowMs)
  if type(ipc) ~= "table" then
    return nil, "publishing a capability report needs an Ipc handle"
  end
  local document = self:report(nowMs)
  local written, writeError = ipc:writeDocument("capabilities", document)
  if written == nil then
    return nil, writeError
  end
  self.published_revision = self.revision
  return document
end

--- True when the report has changed since it was last written.
function Handle:needsPublish()
  return self.published_revision ~= self.revision
end

return CapabilityRuntime
