--[[
PZAgent.Heartbeat -- the tier 0 observation of blueprint 3.6.

Invariant: the heartbeat says what the mod currently believes, and nothing it
has not observed. In particular `build` is the version string the game actually
reported; when the probe could not read one, `build_verified` is false and the
value falls back to the build this release targets rather than being asserted.
The sidecar's watchdog treats a heartbeat that stops arriving as "the game is
gone" (blueprint 8.4), so this must be cheap enough to run on every tick that
needs it -- it reads scalars and writes one small file, never the inventory.

`build` is pure. `write` is the only part that touches disk, and `tick` is the
single call the mod's event wiring makes.
]]

PZAgent = PZAgent or {}

local Heartbeat = {}
PZAgent.Heartbeat = Heartbeat

--- Read the game's build string. Returns nil plus a reason when the API this
--- build exposes is not the one probed for.
function Heartbeat.detectBuild()
  if type(getCore) ~= "function" then
    return nil, "getCore is not available"
  end
  local ok, core = pcall(getCore)
  if not ok or core == nil then
    return nil, "getCore() did not return the core object"
  end
  if type(core.getVersionNumber) ~= "function" then
    return nil, "getCore():getVersionNumber is not available in this build"
  end
  local read, version = pcall(core.getVersionNumber, core)
  if not read or type(version) ~= "string" or #version == 0 then
    return nil, "the game did not report a version string"
  end
  return version
end

--- Build the heartbeat document.
---
--- Pure: everything it reports is passed in. `context` carries
---   session      -- the open session table, or nil
---   safety       -- PZAgent.Safety.snapshot output
---   action       -- PZAgent.Ownership.describe output, or nil
---   seq          -- the heartbeat's own sequence number
---   now_ms       -- wall clock
---   player_present, player_alive
---   build, build_verified
function Heartbeat.build(context)
  local Protocol = PZAgent.Protocol
  local safety = context.safety or {}
  local action = context.action
  local session = context.session

  local document = {
    schema_version = Protocol.SCHEMA_VERSION,
    protocol_version = Protocol.PROTOCOL_VERSION,
    mod_version = Protocol.MOD_VERSION,
    build = context.build or Protocol.TARGET_BUILD,
    build_verified = context.build_verified == true,
    build_supported = Protocol.isSupportedBuild(context.build or Protocol.TARGET_BUILD),
    seq = context.seq or 0,
    timestamp_ms = context.now_ms or 0,
    player_present = context.player_present == true,
    player_alive = context.player_alive == true,
    armed = safety.armed == true,
    mode = safety.mode or Protocol.MODE.OFF,
    danger_level = safety.danger_level or Protocol.DANGER.NONE,
    manual_takeover = safety.manual_takeover == true,
    sidecar_stale = safety.sidecar_stale ~= false,
    action = {
      ownership = (action and action.ownership) or Protocol.OWNERSHIP.NONE,
      busy = (action and action.busy) == true,
      queued = (action and action.total) or 0,
    },
  }

  if session ~= nil then
    document.session_id = session.session_id
    document.nonce = session.game_nonce
    document.sidecar_nonce = session.nonce
    document.generation = session.generation or 0
  end
  if context.last_error ~= nil then
    document.last_error = context.last_error
  end
  return document
end

--- Write the heartbeat to heartbeat.game.json.
function Heartbeat.write(ipc, document)
  return ipc:writeDocument("game_heartbeat", document)
end

--- Compose and write one heartbeat from the wired agent state.
---
--- `agent` is the table PZAgent_Main builds: ipc, safety, session, sequence,
--- plus the last observed player and queue description. Returns the document
--- that was written, or nil plus a reason.
function Heartbeat.tick(agent, nowMs)
  local seq, seqError = agent.sequence:next("event")
  if seq == nil then
    return nil, seqError
  end
  local build, buildError = Heartbeat.detectBuild()
  local document = Heartbeat.build({
    session = agent.session:current(),
    safety = PZAgent.Safety.snapshot(agent.safety, nowMs),
    action = agent.queue_description,
    seq = seq,
    now_ms = nowMs,
    player_present = agent.player_present,
    player_alive = agent.player_alive,
    build = build,
    build_verified = build ~= nil,
    last_error = agent.safety.last_error or buildError,
  })
  local written, writeError = Heartbeat.write(agent.ipc, document)
  if written == nil then
    return nil, writeError
  end
  return document
end

return Heartbeat
