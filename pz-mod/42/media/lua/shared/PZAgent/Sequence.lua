--[[
PZAgent.Sequence -- monotonic sequence counters, one per protocol stream.

Invariant (blueprint 3.4): a number handed out for a stream is strictly greater
than every number handed out for that stream before it, for the whole life of
the session. The sidecar detects a gap by comparing consecutive sequence
numbers and asks for a full snapshot; a counter that repeated or went backwards
would make a gap look like an ordinary tick and a stale record look current.

The set of streams is closed. An unknown name is an error rather than a new
counter, because counters keyed by a caller-supplied string is unbounded memory
in a table that lives as long as the session does.
]]

PZAgent = PZAgent or {}

local Sequence = {}
PZAgent.Sequence = Sequence

--- The four independent streams of blueprint 3.4.
Sequence.STREAMS = { "observation", "command", "ack", "event" }

local STREAM_SET = {}
for index = 1, #Sequence.STREAMS do
  STREAM_SET[Sequence.STREAMS[index]] = true
end

--- 2^53: the last integer a double represents exactly. Past it, `n + 1 == n`
--- and monotonicity silently stops holding, so the counter refuses to issue.
--- At four observations a second this is roughly seventy million years.
local MAX_SEQUENCE = 9007199254740992

Sequence.MAX_SEQUENCE = MAX_SEQUENCE

local Counters = {}
Counters.__index = Counters

--- Create a fresh set of counters, every stream starting below zero so that the
--- first number issued is 0 -- matching the protocol, where seq is a
--- non-negative integer.
function Sequence.new()
  local self = setmetatable({ values = {} }, Counters)
  for index = 1, #Sequence.STREAMS do
    self.values[Sequence.STREAMS[index]] = -1
  end
  return self
end

--- True when `stream` is one of the four protocol streams.
function Sequence.isStream(stream)
  return type(stream) == "string" and STREAM_SET[stream] == true
end

--- The last number issued for `stream`, or nil when none has been.
function Counters:current(stream)
  if not Sequence.isStream(stream) then
    return nil, string.format("unknown stream %q", tostring(stream))
  end
  local value = self.values[stream]
  if value < 0 then
    return nil
  end
  return value
end

--- Issue the next number for `stream`.
function Counters:next(stream)
  if not Sequence.isStream(stream) then
    return nil, string.format("unknown stream %q", tostring(stream))
  end
  local value = self.values[stream] + 1
  if value >= MAX_SEQUENCE then
    return nil, string.format("stream %q exhausted its sequence space", stream)
  end
  self.values[stream] = value
  return value
end

--- Adopt an externally observed sequence number, used after a restart when the
--- journal on disk already contains records this process did not write.
---
--- Only ever moves forward: a lower number is refused rather than applied, so a
--- replayed or reordered record cannot rewind the stream and cause the next
--- issued number to collide with one already on disk.
function Counters:observe(stream, seq)
  if not Sequence.isStream(stream) then
    return false, string.format("unknown stream %q", tostring(stream))
  end
  if type(seq) ~= "number" or seq ~= math.floor(seq) or seq < 0 or seq >= MAX_SEQUENCE then
    return false, "observed sequence must be a non-negative integer below the limit"
  end
  if seq <= self.values[stream] then
    return false, "observed sequence is not ahead of the current one"
  end
  self.values[stream] = seq
  return true
end

--- Reset every counter. Called when a new session is accepted: sequence numbers
--- are session-scoped, and carrying them across would make records from two
--- sessions look like one continuous stream.
function Counters:reset()
  for index = 1, #Sequence.STREAMS do
    self.values[Sequence.STREAMS[index]] = -1
  end
end

--- Snapshot of every counter, for diagnostics and the heartbeat.
function Counters:snapshot()
  local out = {}
  for index = 1, #Sequence.STREAMS do
    local stream = Sequence.STREAMS[index]
    out[stream] = self.values[stream]
  end
  return out
end

return Sequence
