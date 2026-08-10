--[[
Support for the command-path tests.

Two jobs, the same two the observation support file has. First, load the four
modules this task added: the shared harness lists the modules that existed when
it was written, and adding to that list from here keeps this task self-contained.

Second, build the pieces a command test needs and cannot get from the game: an
agent over a mock filesystem, envelopes that are valid by default so a test can
break exactly one field, and adapters whose calls are counted. The counting is
the point of the spy: "the adapter was not called" is the assertion that a gate
holds, and it cannot be made from a double that only records its result.

What the doubles do *not* prove: nothing here exercises a Project Zomboid build.
A spy adapter answers instantly and never touches ISTimedActionQueue, so these
tests cover the lifecycle, the gates and the journal -- never the question of
whether a timed action behaves the way its adapter assumes.
]]

local Support = {}

local MOD_LUA = "pz-mod/42/media/lua/"

local MODULES = {
  -- Compat first: the command modules reference PZAgent.Compat at call time,
  -- and in the game the shared tree loads before any client file does.
  "shared/PZAgent/Compat.lua",
  "client/PZAgent/CommandReader.lua",
  "client/PZAgent/CommandDispatcher.lua",
  "client/PZAgent/CapabilityRuntime.lua",
  "client/PZAgent/ActionRuntime.lua",
}

--- Load the command modules into the global PZAgent namespace.
function Support.loadModules(root)
  for index = 1, #MODULES do
    local path = (root or "") .. MOD_LUA .. MODULES[index]
    local chunk, err = loadfile(path)
    if chunk == nil then
      error("could not load " .. path .. ": " .. tostring(err), 0)
    end
    chunk()
  end
  return PZAgent
end

Support.SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"
Support.OTHER_SESSION = "9c4e1a77-2b3d-4f55-8a90-1122334455aa"
Support.NOW = 1700000000000

local commandCounter = 0

--- A valid command envelope. Every field can be overridden, which is how a test
--- makes exactly one thing wrong.
function Support.command(fields)
  fields = fields or {}
  commandCounter = commandCounter + 1
  local envelope = {
    protocol_version = PZAgent.Protocol.PROTOCOL_VERSION,
    session_id = Support.SESSION,
    seq = commandCounter,
    command_id = string.format("00000000-0000-4000-8000-%012d", commandCounter),
    idempotency_key = string.format("goal-%d-attempt-1", commandCounter),
    issued_at_ms = Support.NOW,
    lease_ms = 5000,
    action = "action.wait",
    args = { game_seconds = 1 },
  }
  for key, value in pairs(fields) do
    envelope[key] = value
  end
  return envelope
end

--- The JSON line the sidecar would append for `command`.
function Support.line(command)
  local text, err = PZAgent.Json.encode(command)
  if text == nil then
    error("could not encode a test command: " .. tostring(err), 0)
  end
  return text .. "\n"
end

--- Put `commands` in the queue file, behind a journal header.
function Support.publish(fs, commands, options)
  options = options or {}
  local parts = {}
  if options.header ~= false then
    parts[#parts + 1] = Support.line({
      type = PZAgent.Ipc.HEADER_TYPE,
      serial = options.serial or 0,
      created_at_ms = Support.NOW,
    })
  end
  for index = 1, #commands do
    local entry = commands[index]
    if type(entry) == "string" then
      parts[#parts + 1] = entry
    else
      parts[#parts + 1] = Support.line(entry)
    end
  end
  local text = table.concat(parts)
  if options.truncate then
    text = text:sub(1, #text - options.truncate)
  end
  fs:put("pz_agent/" .. PZAgent.Ipc.FILES.command_queue, text)
  return text
end

--- Append raw text to the queue file, standing in for a write that finishes
--- what an earlier one left half-written.
function Support.appendRaw(fs, text)
  local path = "pz_agent/" .. PZAgent.Ipc.FILES.command_queue
  fs:put(path, (fs:read(path) or "") .. text)
end

--- Every decoded ack record in the ack journal, header lines excluded.
function Support.acks(fs)
  local lines = fs:lines("pz_agent/" .. PZAgent.Ipc.FILES.command_ack)
  local records = {}
  for index = 1, #lines do
    local record = PZAgent.Json.decode(lines[index])
    if type(record) == "table" and record.type == nil then
      records[#records + 1] = record
    end
  end
  return records
end

--- The last ack in the journal, or nil.
function Support.lastAck(fs)
  local records = Support.acks(fs)
  return records[#records]
end

--- Acks whose status is terminal.
function Support.terminalAcks(fs)
  local records = Support.acks(fs)
  local out = {}
  for index = 1, #records do
    if PZAgent.Protocol.isTerminalStatus(records[index].status) then
      out[#out + 1] = records[index]
    end
  end
  return out
end

--- An adapter that counts its calls.
---
--- `options.evidence` is what a completed run reports; leaving it out is how a
--- test reaches the "finished with nothing observed" branch. `options.polls`
--- makes the adapter take that many polls before it finishes, which is what
--- occupies the single in-flight slot.
function Support.spyAdapter(action, options)
  options = options or {}
  local spy = {
    action = action,
    required_symbols = options.required_symbols or {},
    capability = options.capability,
    args = options.args,
    starts = 0,
    poll_calls = 0,
    interrupts = 0,
    last_args = nil,
  }
  spy.start = function(context)
    spy.starts = spy.starts + 1
    spy.last_args = context.args
    if options.raise then
      error("mock adapter failure", 0)
    end
    if options.refuse then
      return { failed = true, reason_code = options.refuse, detail = "the spy refused" }
    end
    if (options.polls or 0) > 0 then
      context.state.remaining = options.polls
      return { done = false, progress = 0 }
    end
    return { done = true, evidence = options.evidence }
  end
  if (options.polls or 0) > 0 or options.slow then
    spy.poll = function(context)
      spy.poll_calls = spy.poll_calls + 1
      local remaining = (context.state.remaining or 0) - 1
      context.state.remaining = remaining
      if remaining > 0 then
        return { done = false, progress = 1 - (remaining / (options.polls or 1)) }
      end
      return { done = true, evidence = options.evidence }
    end
    spy.interrupt = function()
      spy.interrupts = spy.interrupts + 1
    end
  end
  return spy
end

--- An agent over a mock filesystem, with a session open unless told otherwise.
---
--- `options.armed` arms the safety state the way session.arm would, without
--- going through a command: a test about queueing should not have to run the
--- arming command first.
function Support.agent(Mock, options)
  options = options or {}
  local fs = Mock.newFilesystem()
  local clock = options.clock or function()
    return Support.NOW
  end
  local agent = {
    ipc = PZAgent.Ipc.new({ fileApi = fs.api, clock = clock }),
    safety = PZAgent.Safety.newState(),
    session = PZAgent.Session.new(),
    sequence = PZAgent.Sequence.new(),
    player_present = true,
    player_alive = true,
  }
  if options.session ~= false then
    local decision = agent.session:offer({
      protocol_version = "1.0",
      session_id = options.session_id or Support.SESSION,
      created_at_ms = Support.NOW - 10,
      nonce = options.nonce or "nonce-one",
      mode = "autonomous",
    }, Support.NOW)
    if not decision.accepted then
      error("the test session was refused: " .. tostring(decision.detail), 0)
    end
  end
  PZAgent.Safety.noteSidecarHeartbeat(agent.safety, Support.NOW)
  if options.armed ~= false then
    agent.safety.armed = true
    agent.safety.mode = PZAgent.Protocol.MODE.AUTONOMOUS
  end
  agent.queue_description = PZAgent.Ownership.describe({}, agent.session:id())
  return agent, fs
end

--- An agent plus a runtime whose registry holds exactly `adapters`.
function Support.runtime(Mock, adapters, options)
  options = options or {}
  local agent, fs = Support.agent(Mock, options)
  local dispatcher = PZAgent.CommandDispatcher.new()
  if options.controls then
    local controls = PZAgent.ActionRuntime.controlAdapters()
    for index = 1, #controls do
      local registered, reason = dispatcher:register(controls[index])
      if registered == nil then
        error("a control adapter was refused: " .. tostring(reason), 0)
      end
    end
  end
  for index = 1, #(adapters or {}) do
    local registered, reason = dispatcher:register(adapters[index])
    if registered == nil then
      error("the test adapter was refused: " .. tostring(reason), 0)
    end
  end
  local runtime, runtimeError = PZAgent.ActionRuntime.install(agent, Support.NOW, {
    dispatcher = dispatcher,
    maxWorkPerTick = options.maxWorkPerTick,
    maxPending = options.maxPending,
  })
  if runtime == nil then
    error("the test runtime could not be installed: " .. tostring(runtimeError), 0)
  end
  return agent, fs, runtime, dispatcher
end

return Support
