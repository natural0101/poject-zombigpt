-- Tests for PZAgent.CommandReader: the cursor, the envelope checks and the
-- replay cache.
--
-- The interesting cases are all failures. A partial line that must not be
-- executed, a command from a session that closed, a lease that ran out while
-- the command sat in the file, a redelivery that must not run twice, and a
-- rotation that must not be read as "no new records".

local ROOT = arg[0]:match("^(.*)test_command_reader%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Mock = dofile(ROOT .. "support/mock_game.lua")
local Support = dofile(ROOT .. "support/command_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local CommandReader = PZ.CommandReader
local Protocol = PZ.Protocol
local REASON = Protocol.REASON
local KIND = CommandReader.KIND

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = Support.NOW
local SESSION = Support.SESSION

local function newReader(options)
  local fs = Mock.newFilesystem()
  local ipc = PZ.Ipc.new({ fileApi = fs.api, clock = function()
    return NOW
  end })
  local reader, err = CommandReader.new({
    ipc = ipc,
    maxRecordsPerPoll = options and options.maxRecordsPerPoll,
    maxPartialPolls = options and options.maxPartialPolls,
    maxRemembered = options and options.maxRemembered,
    maxScanLines = options and options.maxScanLines,
  })
  if reader == nil then
    error("could not build a reader: " .. tostring(err), 0)
  end
  return reader, fs
end

Harness.group("construction refuses a reader with nothing to read")
do
  local reader, reason = CommandReader.new({})
  isNil(reader, "a reader without an Ipc handle is not built")
  contains(reason, "Ipc handle", "the reason names what is missing")
end

Harness.group("a complete record is handed out once, with the cursor advanced")
do
  local reader, fs = newReader()
  local command = Support.command()
  local text = Support.publish(fs, { command })
  local entries = reader:poll(SESSION, NOW)
  equal(#entries, 1, "one command was read")
  equal(entries[1].kind, KIND.COMMAND, "the record is a command")
  equal(entries[1].command.command_id, command.command_id, "the command id survived the round trip")
  equal(reader:byteOffset(), #text, "the cursor covers every byte of the file")

  local again = reader:poll(SESSION, NOW)
  equal(#again, 0, "a second poll re-reads nothing")
end

Harness.group("an unfinished trailing line is ignored, then read when it lands")
do
  local reader, fs = newReader()
  local first = Support.command()
  local second = Support.command()
  local secondLine = Support.line(second)
  -- The last line stops eleven bytes short of complete, as a half-flushed write
  -- would leave it.
  Support.publish(fs, { first, secondLine }, { truncate = 11 })

  local entries = reader:poll(SESSION, NOW)
  equal(#entries, 1, "only the finished command is handed out")
  equal(entries[1].command.command_id, first.command_id, "and it is the first one")
  local held = reader:byteOffset()

  Support.appendRaw(fs, secondLine:sub(#secondLine - 10))
  local completed = reader:poll(SESSION, NOW)
  equal(#completed, 1, "the finished line is read on the next poll")
  equal(completed[1].command.command_id, second.command_id, "and it is the second command")
  ok(reader:byteOffset() > held, "the cursor only moved once the line was complete")
end

Harness.group("a fragment that never completes is dropped, not retried forever")
do
  local reader, fs = newReader({ maxPartialPolls = 2 })
  Support.publish(fs, { '{"command_id": "half' })
  for _ = 1, 2 do
    local entries = reader:poll(SESSION, NOW)
    equal(#entries, 0, "the fragment is held while it might still be growing")
  end
  local entries = reader:poll(SESSION, NOW)
  equal(#entries, 1, "past the retry budget the record is given up on")
  equal(entries[1].kind, KIND.DROPPED, "and it is dropped rather than executed")
  contains(entries[1].detail, "did not decode", "the reason says what was wrong with it")
  equal(#reader:poll(SESSION, NOW), 0, "and the cursor moved past it")
end

Harness.group("a command from another session is rejected")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ session_id = Support.OTHER_SESSION }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].kind, KIND.REJECTED, "the record is refused")
  equal(entries[1].reason_code, REASON.STALE_SESSION, "with STALE_SESSION")
end

Harness.group("a command that arrives with no session open is rejected")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command() })
  local entries = reader:poll(nil, NOW)
  equal(entries[1].reason_code, REASON.STALE_SESSION, "no open session is STALE_SESSION")
  contains(entries[1].detail, "no session", "and says so")
end

Harness.group("the protocol major is checked")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ protocol_version = "2.0" }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].reason_code, REASON.INVALID_ARGUMENT, "a different major is refused")
  contains(entries[1].detail, "protocol major", "and the reason names the mismatch")
end

Harness.group("a schema major this build does not speak is refused")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ schema_version = "3.1" }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].reason_code, REASON.INVALID_ARGUMENT, "the schema major is checked too")
  contains(entries[1].detail, "schema major", "and named")
end

Harness.group("envelope fields are checked against the schema's bounds")
do
  local cases = {
    { field = "seq", value = -1, needle = "seq" },
    { field = "idempotency_key", value = string.rep("k", 129), needle = "idempotency_key" },
    { field = "issued_at_ms", value = "soon", needle = "issued_at_ms" },
    { field = "lease_ms", value = 99, needle = "lease_ms" },
    { field = "lease_ms", value = 300001, needle = "lease_ms" },
    { field = "expected_observation_seq", value = 1.5, needle = "expected_observation_seq" },
    { field = "args", value = "duration_ms=1", needle = "args" },
  }
  for index = 1, #cases do
    local case = cases[index]
    local reader, fs = newReader()
    Support.publish(fs, { Support.command({ [case.field] = case.value }) })
    local entries = reader:poll(SESSION, NOW)
    equal(entries[1].reason_code, REASON.INVALID_ARGUMENT, case.field .. " is refused")
    contains(entries[1].detail, case.needle, "the reason names " .. case.field)
  end
end

Harness.group("an action outside the protocol whitelist never becomes a command")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ action = "os.execute" }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].kind, KIND.REJECTED, "the record is refused")
  equal(entries[1].reason_code, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  contains(entries[1].detail, "whitelist", "and the reason says why")
end

Harness.group("a record with no usable id is dropped, because nobody can be acked")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ command_id = "not-a-uuid" }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].kind, KIND.DROPPED, "it is dropped")
  contains(entries[1].detail, "command_id", "and the diagnostic names the missing field")
end

Harness.group("the lease is checked when the record arrives")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command({ issued_at_ms = NOW - 6000, lease_ms = 5000 }) })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].reason_code, REASON.LEASE_EXPIRED, "an expired lease is refused on arrival")
end

Harness.group("a stale stop is still delivered: stopping only ever removes authority")
do
  local reader, fs = newReader()
  Support.publish(fs, {
    Support.command({ action = "safety.stop", issued_at_ms = NOW - 60000, lease_ms = 1000, args = {} }),
  })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].kind, KIND.COMMAND, "safety.stop is exempt from the lease gate")
end

Harness.group("transport records are skipped, not reported")
do
  local reader, fs = newReader()
  Support.publish(fs, {
    Support.line({ type = PZ.Ipc.ROTATED_TYPE, serial = 0, next_serial = 1 }),
    Support.command(),
  })
  local entries = reader:poll(SESSION, NOW)
  equal(#entries, 1, "the rotation marker produced no entry")
  equal(entries[1].kind, KIND.COMMAND, "and the command behind it was still read")
end

Harness.group("a redelivery replays the stored terminal result")
do
  local reader, fs = newReader()
  local command = Support.command()
  Support.publish(fs, { command })
  local entries = reader:poll(SESSION, NOW)
  equal(entries[1].kind, KIND.COMMAND, "the first delivery is a command")

  local stored = {
    schema_version = Protocol.SCHEMA_VERSION,
    session_id = SESSION,
    seq = 4,
    command_id = command.command_id,
    action = command.action,
    status = Protocol.STATUS.SUCCEEDED,
    reason_code = REASON.POSTCONDITION_MET,
    timestamp_ms = NOW,
    evidence = { waited_ms = 0 },
  }
  ok(reader:remember(command.command_id, stored), "the terminal result is remembered")

  Support.appendRaw(fs, Support.line(command))
  local again = reader:poll(SESSION, NOW + 10)
  equal(again[1].kind, KIND.REPLAY, "the redelivery replays")
  equal(again[1].ack.status, Protocol.STATUS.SUCCEEDED, "with the original outcome")
  equal(again[1].ack.evidence.waited_ms, 0, "and the original evidence")
end

Harness.group("a retry under the same idempotency key replays too")
do
  local reader, fs = newReader()
  local first = Support.command()
  Support.publish(fs, { first })
  reader:poll(SESSION, NOW)
  reader:remember(first.command_id, {
    schema_version = Protocol.SCHEMA_VERSION,
    session_id = SESSION,
    seq = 1,
    command_id = first.command_id,
    action = first.action,
    status = Protocol.STATUS.FAILED,
    reason_code = REASON.NO_SAFE_FOOD,
    timestamp_ms = NOW,
  })

  local retry = Support.command({ idempotency_key = first.idempotency_key })
  Support.appendRaw(fs, Support.line(retry))
  local entries = reader:poll(SESSION, NOW + 5)
  equal(entries[1].kind, KIND.REPLAY, "the retry replays the first attempt's result")
  equal(entries[1].ack.reason_code, REASON.NO_SAFE_FOOD, "including its reason code")
  equal(entries[1].command.command_id, retry.command_id, "and carries the retry's own id")
end

Harness.group("a redelivery of a command still running is neither replayed nor rerun")
do
  local reader, fs = newReader()
  local command = Support.command()
  Support.publish(fs, { command })
  reader:poll(SESSION, NOW)
  Support.appendRaw(fs, Support.line(command))
  local entries = reader:poll(SESSION, NOW + 1)
  equal(entries[1].kind, KIND.DUPLICATE, "it is a duplicate of work in progress")
  contains(entries[1].detail, "terminal", "and the detail says the original has not finished")
end

Harness.group("a non-terminal result is refused by the replay cache")
do
  local reader = newReader()
  local stored, reason = reader:remember("00000000-0000-4000-8000-000000000001", {
    status = Protocol.STATUS.STARTED,
  })
  isNil(stored, "a started ack is not a result to replay")
  contains(reason, "not terminal", "and the reason says so")
end

Harness.group("the replay cache is bounded")
do
  local reader, fs = newReader({ maxRemembered = 4 })
  local commands = {}
  for index = 1, 6 do
    commands[index] = Support.command()
  end
  Support.publish(fs, commands)
  -- Seven lines: the journal header spends one of the budget too.
  local entries = reader:poll(SESSION, NOW, 7)
  equal(#entries, 6, "all six were read")
  equal(reader:rememberedCount(), 4, "but only four are remembered")

  Support.appendRaw(fs, Support.line(commands[1]))
  local again = reader:poll(SESSION, NOW)
  equal(again[1].kind, KIND.COMMAND, "the evicted command is no longer recognised as a duplicate")
end

Harness.group("the batch size is bounded")
do
  local reader, fs = newReader({ maxRecordsPerPoll = 3 })
  local commands = {}
  for index = 1, 10 do
    commands[index] = Support.command()
  end
  Support.publish(fs, commands)
  -- Three lines, of which the first is the journal header.
  equal(#reader:poll(SESSION, NOW, 99), 2, "a caller cannot ask for more than the cap")
  equal(#reader:poll(SESSION, NOW, 99), 3, "and the cap holds on every poll")
  equal(#reader:poll(SESSION, NOW, 2), 2, "a caller may ask for less")
  equal(#reader:poll(SESSION, NOW, 0), 0, "a zero budget reads nothing")
end

Harness.group("a rotation resets the cursor and is reported as loss")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command(), Support.command() }, { serial = 0 })
  equal(#reader:poll(SESSION, NOW, 8), 2, "both records of the first generation were read")

  local fresh = Support.command()
  Support.publish(fs, { fresh }, { serial = 1 })
  local entries, diagnostic = reader:poll(SESSION, NOW, 8)
  equal(#entries, 1, "the new generation is read from its first record")
  equal(entries[1].command.command_id, fresh.command_id, "and it is the record the sidecar wrote")
  contains(diagnostic, "rotated", "the rotation is reported")
  contains(diagnostic, "lost", "as loss, not as a quiet reset")
end

Harness.group("the scan budget stops the cursor from walking an unbounded file")
do
  local reader, fs = newReader({ maxScanLines = 4 })
  local commands = {}
  for index = 1, 4 do
    commands[index] = Support.command()
  end
  Support.publish(fs, commands)
  equal(#reader:poll(SESSION, NOW, 4), 3, "the first batch fits inside the budget")
  local entries, reason = reader:poll(SESSION, NOW, 4)
  isNil(entries, "past it the reader refuses to walk the file again")
  contains(reason, "scan budget", "and says why")
end

Harness.group("an unreadable queue is a reason, never an empty stream")
do
  local reader, fs = newReader()
  Support.publish(fs, { Support.command() })
  fs:failReadsFrom("pz_agent/" .. PZ.Ipc.FILES.command_queue)
  local entries, reason = reader:poll(SESSION, NOW)
  isNil(entries, "the poll fails")
  contains(reason, "mock read failure", "with the reason the file API gave")
end

Harness.finish("test_command_reader")
