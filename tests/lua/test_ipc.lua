-- Transport tests for PZAgent.Ipc.
--
-- Two properties carry the design and both are checked here: a caller can only
-- ever name a file by role, and the snapshot pointer is written after the slot
-- it points at.

local Harness = dofile((arg[0]:match("^(.*)test_ipc%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_ipc%.lua$") or "") .. "support/mock_game.lua")
local PZ = Harness.loadModules()
local Ipc = PZ.Ipc
local Json = PZ.Json

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = 1700000000000

local function newHandle(options)
  local fs = Mock.newFilesystem()
  options = options or {}
  options.fileApi = fs.api
  options.clock = options.clock or function()
    return NOW
  end
  return Ipc.new(options), fs
end

local function path(role)
  return Ipc.pathFor(role)
end

Harness.group("filenames are constants, never arguments")
do
  equal(Ipc.pathFor("game_heartbeat"), "pz_agent/heartbeat.game.json", "the heartbeat path")
  equal(Ipc.pathFor("command_queue"), "pz_agent/command.queue.0001.jsonl", "the command queue path")
  equal(Ipc.pathFor("snapshot_pointer"), "pz_agent/observation.snapshot.pointer", "the pointer path")
  equal(Ipc.pathFor("panic_stop"), "pz_agent/panic.stop", "the panic stop path")

  isNil(Ipc.pathFor("../../etc/passwd"), "a path is not a role")
  isNil(Ipc.pathFor("pz_agent/heartbeat.game.json"), "a filename is not a role either")
  isNil(Ipc.pathFor("unknown_role"), "an unknown role has no path")
  isNil(Ipc.pathFor(nil), "a nil role has no path")
  isNil(Ipc.pathFor(42), "a non-string role has no path")
  contains(select(2, Ipc.pathFor("../escape")), "unknown file role", "the refusal names the problem")

  local handle = newHandle()
  isNil(handle:writeDocument("../escape", { a = 1 }), "writing to an unknown role is refused")
  isNil(handle:readLines("../escape"), "reading from an unknown role is refused")
end

Harness.group("documents round-trip through the file API")
do
  local handle, fs = newHandle()
  local written = handle:writeDocument("game_heartbeat", { seq = 3, armed = false })
  ok(written ~= nil, "the document was written")
  equal(fs:read(path("game_heartbeat")), '{"armed":false,"seq":3}', "and its bytes are the deterministic encoding")

  local read = handle:readDocument("game_heartbeat")
  equal(read.seq, 3, "the document reads back")
  equal(read.armed, false, "including a false value, which is not a read failure")

  isNil(handle:readDocument("session"), "a file that does not exist is not a document")
  contains(select(2, handle:readDocument("session")), "empty or absent", "and says so")

  fs:put(path("session"), "{not json")
  isNil(handle:readDocument("session"), "a torn document is refused rather than half-parsed")

  isNil(handle:writeDocument("game_heartbeat", { fn = print }), "an unencodable document is refused")
end

Harness.group("failures are reported, never swallowed")
do
  local handle, fs = newHandle()
  fs:failWritesTo(path("game_heartbeat"))
  local written, err = handle:writeDocument("game_heartbeat", { seq = 1 })
  isNil(written, "a failing write returns no byte count")
  contains(err, "opening", "and explains what failed")

  fs:put(path("session"), "{}")
  fs:failReadsFrom(path("session"))
  isNil(handle:readLines("session"), "a failing read is reported")

  -- Outside the game the engine globals are absent, which is exactly the state
  -- a handle created with no injected API finds itself in.
  local outsideGame = Ipc.new()
  ok(not outsideGame:isAvailable(), "with no engine file API the handle reports itself unavailable")
  local attempted, apiError = outsideGame:writeDocument("game_heartbeat", {})
  isNil(attempted, "and nothing is written")
  contains(apiError, "getFileWriter", "with a reason naming the missing global")
  isNil(outsideGame:readLines("session"), "reads are refused the same way")

  local badApi = Ipc.new({ fileApi = false })
  ok(not badApi:isAvailable(), "an injected API that is not a table is refused")
  contains(select(2, badApi:writeDocument("game_heartbeat", {})), "must be a table", "and says why")
end

Harness.group("snapshots alternate slots and commit with the pointer")
do
  local handle, fs = newHandle()
  local slot = handle:publishSnapshot({ seq = 1, full = true })
  equal(slot, "a", "the first snapshot goes to slot a")
  ok(fs:read(path("snapshot_a")) ~= nil, "slot a was written")
  isNil(fs:read(path("snapshot_b")), "slot b was left alone")

  -- The ordering is the entire correctness argument: a reader following the
  -- pointer must never be sent to a file that is still being written.
  ok(
    fs:lastWriteIndex(path("snapshot_pointer")) > fs:lastWriteIndex(path("snapshot_a")),
    "the pointer is written after the slot it points at"
  )

  local pointer = handle:readDocument("snapshot_pointer")
  equal(pointer.slot, "a", "the pointer names slot a")
  equal(pointer.seq, 1, "and carries the sequence number")
  equal(pointer.written_at_ms, NOW, "and when it was written")

  equal(handle:publishSnapshot({ seq = 2 }), "b", "the next snapshot goes to the other slot")
  equal(handle:publishSnapshot({ seq = 3 }), "a", "and then back again")
  equal(handle:readDocument("snapshot_a").seq, 3, "slot a now holds the newest snapshot")
  equal(handle:readDocument("snapshot_b").seq, 2, "slot b still holds the previous one")

  -- A restarted mod must not overwrite the slot a reader is about to follow, so
  -- the next slot comes from the pointer on disk, not from memory.
  local restarted = Ipc.new({ fileApi = fs.api, clock = function() return NOW end })
  equal(restarted:publishSnapshot({ seq = 4 }), "b", "a fresh handle continues the alternation")

  fs:put(path("snapshot_pointer"), "{tor")
  equal(handle:publishSnapshot({ seq = 5 }), "a", "an unreadable pointer restarts at slot a")

  isNil(handle:publishSnapshot({ full = true }), "a snapshot without a seq is refused")
  isNil(handle:publishSnapshot({ seq = -1 }), "a negative seq is refused")
  isNil(handle:publishSnapshot({ seq = 1.5 }), "a fractional seq is refused")
  isNil(handle:publishSnapshot("not a table"), "a non-table snapshot is refused")
end

Harness.group("journals append one complete line per record")
do
  local handle, fs = newHandle()
  ok(handle:appendRecord("observation_events", { type = "safety.stop", seq = 1 }) ~= nil, "a record appends")
  ok(handle:appendRecord("observation_events", { type = "safety.stop", seq = 2 }) ~= nil, "and another")

  local lines = fs:lines(path("observation_events"))
  equal(#lines, 3, "a header line plus the two records")
  local header = Json.decode(lines[1])
  equal(header.type, "journal.header", "the first line is the journal header the reader expects")
  equal(header.serial, 0, "with the first serial")
  equal(Json.decode(lines[2]).seq, 1, "then the first record")
  equal(Json.decode(lines[3]).seq, 2, "then the second")

  local content = fs:read(path("observation_events"))
  equal(content:sub(-1), "\n", "the file ends with the newline that commits the last record")

  isNil(handle:appendRecord("observation_events", { type = "journal.header" }), "a forged header is refused")
  isNil(handle:appendRecord("observation_events", { type = "journal.rotated" }), "a forged rotation is refused")
  isNil(handle:appendRecord("observation_events", { fn = print }), "an unencodable record is refused")
end

Harness.group("a journal resumes the serial an earlier run left behind")
do
  local handle, fs = newHandle()
  fs:put(path("command_ack"), '{"created_at_ms":0,"serial":7,"type":"journal.header"}\n{"seq":1}\n')
  ok(handle:appendRecord("command_ack", { seq = 2 }) ~= nil, "the record appends to the existing file")
  local lines = fs:lines(path("command_ack"))
  equal(#lines, 3, "no second header was written")
  equal(Json.decode(lines[1]).serial, 7, "the existing serial is kept, so the reader is not told the stream rewound")
end

Harness.group("a journal is bounded, and says so when it wraps")
do
  local handle, fs = newHandle({ maxJournalBytes = 400 })
  for index = 1, 20 do
    handle:appendRecord("observation_events", { seq = index, note = "padding-padding-padding" })
  end
  local lines = fs:lines(path("observation_events"))
  ok(#lines < 21, "the file did not grow without bound")
  local header = Json.decode(lines[1])
  equal(header.type, "journal.header", "the restarted file begins with a header")
  ok(header.serial > 0, "whose serial advanced, which is how the reader learns records were lost")
  ok(handle:journalBytes("observation_events") <= 400 + 200, "the tracked size stays near the cap")
end

Harness.group("the panic stop file is a flag, never an instruction")
do
  local handle, fs = newHandle()
  ok(not handle:panicStopRequested(), "an absent file is not a request")

  fs:put(path("panic_stop"), "")
  ok(not handle:panicStopRequested(), "an empty file is not a request either")

  fs:put(path("panic_stop"), '{"reason":"whatever"}\n')
  ok(handle:panicStopRequested(), "any content at all is a request")

  fs:put(path("panic_stop"), "action: consume.eat\n")
  ok(handle:panicStopRequested(), "content that looks like a command is still only a stop request")

  ok(handle:clearPanicStop() ~= nil, "the request can be cleared")
  ok(not handle:panicStopRequested(), "and is gone afterwards")

  ok(handle:requestPanicStop("PANIC_STOP") ~= nil, "the mod can raise a request of its own")
  ok(handle:panicStopRequested(), "which reads back as a request")
  equal(Json.decode(fs:lines(path("panic_stop"))[1]).source, "mod", "and records where it came from")

  handle:clearPanicStop()
  ok(handle:requestPanicStop() ~= nil, "a request with no reason given still writes")
  equal(
    Json.decode(fs:lines(path("panic_stop"))[1]).reason,
    "PANIC_STOP",
    "and defaults to the panic stop reason code"
  )
end

Harness.group("reads are bounded")
do
  local handle, fs = newHandle({ maxLines = 3 })
  local body = {}
  for index = 1, 50 do
    body[index] = '{"seq":' .. index .. "}"
  end
  fs:put(path("command_queue"), table.concat(body, "\n") .. "\n")
  equal(#handle:readLines("command_queue"), 3, "a poll stops at the configured line limit")
  equal(#handle:readLines("command_queue", 10), 10, "the limit can be raised per call")
  equal(#handle:readLines("command_queue", 5, 45), 5, "and a poll can skip lines it already consumed")
  equal(Json.decode(handle:readLines("command_queue", 1, 9)[1]).seq, 10, "skipping lands on the right record")
end

Harness.finish("test_ipc")
