-- Counter tests for PZAgent.Sequence.
--
-- The sidecar detects a gap by comparing consecutive numbers, so the property
-- under test is not "the counter increments" but "it never repeats and never
-- goes backwards", including through the recovery paths.

local Harness = dofile((arg[0]:match("^(.*)test_sequence%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Sequence = PZ.Sequence

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

Harness.group("streams are a closed set")
do
  equal(#Sequence.STREAMS, 4, "the four streams of blueprint 3.4")
  for index = 1, #Sequence.STREAMS do
    ok(Sequence.isStream(Sequence.STREAMS[index]), Sequence.STREAMS[index] .. " is a stream")
  end
  ok(not Sequence.isStream("telemetry"), "an unlisted stream name is not a stream")
  ok(not Sequence.isStream(nil), "nil is not a stream")

  local counters = Sequence.new()
  isNil(counters:next("telemetry"), "an unknown stream cannot be issued from")
  contains(select(2, counters:next("telemetry")), "unknown stream", "the refusal names the problem")
  isNil(counters:current("telemetry"), "an unknown stream has no current value")
  ok(not counters:observe("telemetry", 1), "an unknown stream cannot be observed into")
end

Harness.group("issued numbers are strictly increasing")
do
  local counters = Sequence.new()
  isNil(counters:current("observation"), "nothing has been issued yet")
  equal(counters:next("observation"), 0, "the first number is zero")
  equal(counters:next("observation"), 1, "the second is one")
  equal(counters:current("observation"), 1, "current follows the last issued")

  local previous = counters:next("observation")
  for _ = 1, 200 do
    local value = counters:next("observation")
    ok(value == previous + 1, "each number is exactly one above the last")
    previous = value
  end
end

Harness.group("streams are independent")
do
  local counters = Sequence.new()
  counters:next("observation")
  counters:next("observation")
  counters:next("observation")
  equal(counters:next("command"), 0, "the command stream starts at zero regardless of observation")
  equal(counters:next("ack"), 0, "so does the ack stream")
  equal(counters:current("observation"), 2, "the observation stream is unaffected")
  local snapshot = counters:snapshot()
  equal(snapshot.observation, 2, "the snapshot reports observation")
  equal(snapshot.command, 0, "the snapshot reports command")
  equal(snapshot.event, -1, "an untouched stream reports below zero")
end

Harness.group("observing an external number only ever moves forward")
do
  local counters = Sequence.new()
  ok(counters:observe("ack", 100), "a number ahead of the counter is adopted")
  equal(counters:next("ack"), 101, "issuing continues above the adopted number")
  ok(not counters:observe("ack", 50), "a lower number is refused")
  equal(counters:current("ack"), 101, "the refusal left the counter alone")
  ok(not counters:observe("ack", 101), "the current number is not ahead of itself")
  ok(not counters:observe("ack", -1), "a negative number is refused")
  ok(not counters:observe("ack", 1.5), "a fractional number is refused")
  ok(not counters:observe("ack", "7"), "a string is refused")
  ok(not counters:observe("ack", Sequence.MAX_SEQUENCE), "a number at the limit is refused")
end

Harness.group("reset scopes counters to a session")
do
  local counters = Sequence.new()
  counters:next("observation")
  counters:next("command")
  counters:reset()
  isNil(counters:current("observation"), "reset clears the observation stream")
  equal(counters:next("observation"), 0, "a reset stream starts over at zero")
  equal(counters:next("command"), 0, "so does every other stream")
end

Harness.group("the counter refuses to wrap")
do
  local counters = Sequence.new()
  counters.values.event = Sequence.MAX_SEQUENCE - 2
  equal(counters:next("event"), Sequence.MAX_SEQUENCE - 1, "the last representable number is issued")
  isNil(counters:next("event"), "past it the counter refuses rather than repeating")
  contains(select(2, counters:next("event")), "exhausted", "the refusal explains itself")
  equal(counters:current("event"), Sequence.MAX_SEQUENCE - 1, "a refused issue does not advance the counter")
end

Harness.finish("test_sequence")
