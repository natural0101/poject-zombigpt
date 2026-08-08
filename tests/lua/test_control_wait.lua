-- Tests for the action.wait control adapter against a readable world clock.
--
-- The wire shape is {"game_seconds": number}, and the wait is measured against
-- the game's world clock -- the same `game.world_time` string observations
-- carry and the sidecar verifies against. These tests install a fake
-- getGameTime whose calendar the test advances by hand, so the one thing they
-- prove is the thing defect audits found broken: the mod waits on *game* time,
-- in the unit the sidecar sends, and never on the real milliseconds of the
-- tick clock.
--
-- test_action_runtime.lua keeps the other half: without a readable clock the
-- wait refuses as CAPABILITY_UNAVAILABLE, and the old `duration_ms` key is
-- refused at the dispatcher.

local ROOT = arg[0]:match("^(.*)test_control_wait%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Mock = dofile(ROOT .. "support/mock_game.lua")
local Support = dofile(ROOT .. "support/command_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

-- The world-clock path: ObserveModel formats the reading, Observe reads it.
-- Loaded here exactly as the game loads them, shared before client.
dofile(Harness.root .. "pz-mod/42/media/lua/shared/PZAgent/ObserveModel.lua")
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/Observe.lua")

local Protocol = PZ.Protocol
local REASON = Protocol.REASON
local STATUS = Protocol.STATUS

local equal, isNil, contains = Harness.equal, Harness.isNil, Harness.contains
local NOW = Support.NOW

--- A mutable in-game calendar, installed as the getGameTime global. The test
--- moves `clock` fields; nothing else does.
local clock = { year = 1993, month = 7, day = 9, hour = 13, minute = 0 }
local function installClock()
  local gameTime = {
    getMultiplier = function()
      return 1
    end,
    getYear = function()
      return clock.year
    end,
    getMonth = function()
      return clock.month
    end,
    getDay = function()
      return clock.day
    end,
    getHour = function()
      return clock.hour
    end,
    getMinutes = function()
      return clock.minute
    end,
  }
  getGameTime = function()
    return gameTime
  end
end

local function lastTerminal(fs)
  local records = Support.terminalAcks(fs)
  return records[#records]
end

installClock()

Harness.group("action.wait waits on the world clock, not on real milliseconds")
do
  clock.hour, clock.minute = 13, 0
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, {
    Support.command({ action = "action.wait", args = { game_seconds = 120 }, lease_ms = 300000 }),
  })
  runtime:tick(agent, NOW)
  equal(#Support.terminalAcks(fs), 0, "the wait is in flight")

  -- Three real seconds pass; the world clock does not move. The old adapter
  -- counted these milliseconds, and none of them may count.
  runtime:tick(agent, NOW + 3000)
  equal(#Support.terminalAcks(fs), 0, "real time alone finishes nothing")

  clock.minute = 1
  runtime:tick(agent, NOW + 3001)
  equal(#Support.terminalAcks(fs), 0, "one game minute is short of the two asked for")

  clock.minute = 2
  runtime:tick(agent, NOW + 3002)
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "two game minutes finished the wait")
  equal(terminal.evidence.world_time_before, "1993-07-09T13:00", "on the reading it started from")
  equal(terminal.evidence.world_time_after, "1993-07-09T13:02", "and the reading that satisfied it")
  equal(terminal.evidence.elapsed_game_seconds, 120, "counting game seconds")
  equal(terminal.evidence.requested_game_seconds, 120, "against what was asked for")
end

Harness.group("a wait crossing midnight still counts its game seconds")
do
  clock.day, clock.hour, clock.minute = 9, 23, 59
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, {
    Support.command({ action = "action.wait", args = { game_seconds = 120 }, lease_ms = 300000 }),
  })
  runtime:tick(agent, NOW)
  equal(#Support.terminalAcks(fs), 0, "the wait is in flight")

  clock.day, clock.hour, clock.minute = 10, 0, 1
  runtime:tick(agent, NOW + 1)
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "the date change did not lose the elapsed time")
  equal(terminal.evidence.elapsed_game_seconds, 120, "23:59 to 00:01 is two game minutes")
end

Harness.group("a clock that went backwards never accumulates towards success")
do
  clock.day, clock.hour, clock.minute = 10, 8, 30
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, {
    Support.command({ action = "action.wait", args = { game_seconds = 60 }, lease_ms = 300000 }),
  })
  runtime:tick(agent, NOW)

  -- A reload put the world clock in the past. That is not a wait, and it must
  -- not read as one -- not even as partial progress.
  clock.hour = 7
  runtime:tick(agent, NOW + 1)
  equal(#Support.terminalAcks(fs), 0, "the reversed clock finished nothing")

  -- What finally ends a wait whose clock never comes back is a real-time
  -- bound, never a success: here the lease runs out first, because a wait's
  -- own 300000 ms ceiling can never undercut the longest lease a command may
  -- carry. The heartbeat is refreshed so the lease is the bound being proven.
  PZ.Safety.noteSidecarHeartbeat(agent.safety, NOW + 299000)
  runtime:tick(agent, NOW + 300001)
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.CANCELLED, "the wait was taken away, not completed")
  equal(terminal.reason_code, REASON.LEASE_EXPIRED, "by its lease running out")
end

Harness.group("a zero wait is refused by the adapter, not rounded up")
do
  clock.day, clock.hour, clock.minute = 10, 9, 0
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "action.wait", args = { game_seconds = 0 } }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.INVALID_ARGUMENT, "zero game seconds is not a wait")
  contains(terminal.message, "greater than zero", "and the message says what the bound is")
end

Harness.group("a wait past one in-game hour is refused at the declaration")
do
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "action.wait", args = { game_seconds = 3601 } }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.REJECTED, "the command never reached the adapter")
  equal(terminal.reason_code, REASON.INVALID_ARGUMENT, "a longer wait is a plan step, not an action")
end

Harness.group("the in-flight wait reports progress as the clock advances")
do
  clock.day, clock.hour, clock.minute = 10, 10, 0
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, {
    Support.command({ action = "action.wait", args = { game_seconds = 240 }, lease_ms = 300000 }),
  })
  runtime:tick(agent, NOW)

  clock.minute = 2
  runtime:tick(agent, NOW + 1)
  local records = Support.acks(fs)
  local progressed = records[#records]
  equal(progressed.status, STATUS.PROGRESS, "half the wait is a progress ack")
  equal(progressed.progress, 0.5, "carrying the observed fraction")
  isNil(lastTerminal(fs), "and the wait is still in flight")

  clock.minute = 4
  runtime:tick(agent, NOW + 2)
  equal(lastTerminal(fs).status, STATUS.SUCCEEDED, "until the clock has covered it")
end

getGameTime = nil

Harness.finish("test_control_wait")
