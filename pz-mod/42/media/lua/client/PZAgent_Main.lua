--[[
PZAgent_Main -- event wiring, and nothing else.

Blueprint 11.5 makes this file's smallness a requirement: every decision lives
in a module under shared/PZAgent or client/PZAgent so it can be loaded and
tested outside the game. What is left here is the part that cannot be tested
outside the game -- which handler hangs off which engine event.

Load order: the game loads media/lua/shared before media/lua/client, so every
PZAgent.* module exists by the time this file runs. Nothing runs at load time
beyond registering handlers; the agent is built on OnGameStart and starts
disarmed.

Registration is guarded. An event this build does not expose is recorded in
PZAgent.unavailableEvents and skipped, because a missing event must degrade one
feature rather than abort the whole file and leave the mod half-loaded.
]]

PZAgent = PZAgent or {}

--- DirectInput scancode for F12, the default panic hotkey (blueprint 11.8).
--- Hardcoded rather than read from a Keyboard global whose presence in this
--- build has not been probed.
local PANIC_KEY = 88

--- Heartbeat cadence. Ten ticks is well inside the sidecar's staleness window
--- while leaving the game thread room for everything else.
local HEARTBEAT_TICK_INTERVAL = 10

PZAgent.unavailableEvents = {}

local agent = nil
local tickCounter = 0

local function now()
  if type(getTimestampMs) == "function" then
    return getTimestampMs()
  end
  return 0
end

local function register(eventName, handler)
  local event = Events and Events[eventName]
  if type(event) ~= "table" or type(event.Add) ~= "function" then
    PZAgent.unavailableEvents[#PZAgent.unavailableEvents + 1] = eventName
    return false
  end
  event.Add(handler)
  return true
end

local function hudState()
  return {
    safety = PZAgent.Safety.snapshot(agent.safety, now()),
    session = agent.session:current(),
    last_error = agent.safety.last_error,
  }
end

local function start()
  agent = {
    ipc = PZAgent.Ipc.new(),
    safety = PZAgent.Safety.newState(),
    session = PZAgent.Session.new(),
    sequence = PZAgent.Sequence.new(),
    player_present = false,
    player_alive = false,
  }
  local hud, hudError = PZAgent.Hud.create(hudState)
  agent.hud = hud
  if hud == nil then
    agent.safety.last_error = hudError
  end
  PZAgent.Runtime.refresh(agent, now())
end

local function shutdown()
  if agent == nil then
    return
  end
  PZAgent.Runtime.stop(agent, now(), PZAgent.Protocol.REASON.SESSION_TERMINATED)
  local removed, removeError = PZAgent.Hud.destroy(agent.hud)
  if not removed and agent.hud ~= nil then
    agent.safety.last_error = removeError
  end
  agent = nil
end

local function onTick()
  if agent == nil then
    return
  end
  tickCounter = tickCounter + 1
  if tickCounter < HEARTBEAT_TICK_INTERVAL then
    return
  end
  tickCounter = 0
  PZAgent.Runtime.tick(agent, now())
end

local function onKeyPressed(key)
  if agent ~= nil and key == PANIC_KEY then
    PZAgent.Runtime.stop(agent, now(), PZAgent.Protocol.REASON.PANIC_STOP)
  end
end

local function onMovementInput(player)
  if agent ~= nil then
    PZAgent.Runtime.noteInput(agent, player, "movement", now())
  end
end

local function onAttackInput(player)
  if agent ~= nil then
    PZAgent.Runtime.noteInput(agent, player, "attack", now())
  end
end

register("OnGameStart", start)
register("OnPlayerDeath", shutdown)
register("OnTick", onTick)
register("OnKeyPressed", onKeyPressed)
register("OnPlayerMove", onMovementInput)
register("OnPlayerAttackFinished", onAttackInput)
