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

  -- The warm handle last committed slot a (seq 3) and alternates from that
  -- memory: it never re-opens the pointer it is about to truncate, so torn
  -- bytes on disk cannot derail it -- and on Windows it no longer collides
  -- with its own read's sharing lock.
  fs:put(path("snapshot_pointer"), "{tor")
  equal(handle:publishSnapshot({ seq = 5 }), "b", "a warm handle alternates from its own last commit, not the disk")

  fs:put(path("snapshot_pointer"), "{tor")
  local blank = Ipc.new({ fileApi = fs.api, clock = function() return NOW end })
  equal(blank:publishSnapshot({ seq = 6 }), "a", "an unreadable pointer restarts a fresh handle at slot a")

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

Harness.group("the journal cap survives a mod reload")
do
  -- A reloaded mod builds a new handle over a file that is already there. A
  -- handle that started counting from zero would let the file grow by another
  -- whole cap on every reload, which is unbounded growth over a long session.
  local first, fs = newHandle({ maxJournalBytes = 2000 })
  for index = 1, 12 do
    first:appendRecord("observation_events", { seq = index, note = "padding-padding-padding-padding" })
  end
  local sizeAfterFirst = #fs:read(path("observation_events"))
  ok(sizeAfterFirst <= 2000, "the first handle kept the file inside the cap")

  local resumed = Ipc.new({ fileApi = fs.api, maxJournalBytes = 2000, clock = function()
    return NOW
  end })
  ok(
    resumed:journalBytes("observation_events") == 0,
    "a fresh handle has appended nothing yet"
  )
  ok(resumed:appendRecord("observation_events", { seq = 13 }) ~= nil, "and its first record appends")
  ok(
    resumed:journalBytes("observation_events") >= sizeAfterFirst,
    "but it adopted the size of the file it found, instead of starting at zero"
  )

  for index = 14, 40 do
    resumed:appendRecord("observation_events", { seq = index, note = "padding-padding-padding-padding" })
  end
  ok(#fs:read(path("observation_events")) <= 2000, "so the cap still holds after the reload")

  -- A journal whose size cannot be measured at all -- too large to read, or an
  -- I/O error part way through -- is assumed to be at the cap. Assuming it is
  -- empty is what would let it grow without bound.
  local _, blindFs = newHandle()
  blindFs:put(path("command_ack"), '{"created_at_ms":0,"serial":3,"type":"journal.header"}\n{"seq":1}\n')
  -- The header read succeeds; the read that measures the file does not.
  blindFs:failReadsFrom(path("command_ack"), 1)
  local blind = Ipc.new({ fileApi = blindFs.api, maxJournalBytes = 2000, clock = function()
    return NOW
  end })
  ok(blind:appendRecord("command_ack", { seq = 2 }) ~= nil, "a record still appends")
  local restarted = Json.decode(blindFs:lines(path("command_ack"))[1])
  equal(restarted.type, "journal.header", "onto a restarted file")
  equal(restarted.serial, 4, "whose serial advanced, so the reader is told records were lost")
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

Harness.group("the byte caps bound a record, a document and a read")
do
  -- Three caps, all in this module and none of them tested. The mod writes and
  -- reads these files inside a game tick, so an unbounded one is a frame-rate
  -- bug at best and a Lua memory failure at worst -- and the sidecar's own
  -- reader refuses an oversized line, so a record written past the cap is
  -- accepted locally and rejected at the far end: the journal keeps a record
  -- nobody will ever read, and the mod believes it published.
  --
  -- The bounds are read from the module rather than copied, so raising one
  -- without meaning to still leaves this group honest.
  local handle, fs = newHandle()

  -- 1. A journal record past the line cap is refused, and nothing lands.
  local before = #fs:lines(path("observation_events"))
  local huge = string.rep("x", Ipc.MAX_LINE_BYTES)
  local appended, appendError = handle:appendRecord("observation_events", {
    type = "safety.stop",
    blob = huge,
  })
  isNil(appended, "a record past the line cap is refused")
  contains(appendError or "", "line limit", "and the refusal says which limit")
  equal(#fs:lines(path("observation_events")), before,
    "with nothing appended -- a line the sidecar would refuse is not written")

  -- 2. A whole document past the document cap is refused.
  local written, writeError = handle:writeDocument("capabilities", {
    blob = string.rep("y", Ipc.MAX_DOCUMENT_BYTES),
  })
  isNil(written, "a document past the cap is refused")
  contains(writeError or "", "byte limit", "and says so")

  -- 3. A read stops at the cap rather than pulling the file into memory. The
  -- file here is written past the bound directly, which is the only way it can
  -- get there: the writer above refuses to produce one.
  fs:put(path("command_queue"), string.rep("z", Ipc.MAX_DOCUMENT_BYTES + 1) .. "\n")
  local lines, readError = handle:readLines("command_queue", 10)
  ok(lines == nil or #lines == 0, "an oversized file yields no records")
  contains(readError or "", "read limit", "and the read reports the bound it hit")
end

-- ---------------------------------------------------------------------------
-- Windows sharing locks: a refused open is a race lost, not a lost publish.
-- The wrappers below stand in for getFileWriter/getFileReader answering nil
-- while someone else briefly holds the file.
-- ---------------------------------------------------------------------------

Harness.group("a refused writer open is retried inside the call, boundedly")
do
  local fs = Mock.newFilesystem()
  local writerOpens = 0
  local writerRefusals = 2
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openReader = function(name) return fs.api.openReader(name) end,
      openWriter = function(name, append)
        writerOpens = writerOpens + 1
        if writerRefusals > 0 then
          writerRefusals = writerRefusals - 1
          return nil -- a sharing lock: getFileWriter answers nil, no raise
        end
        return fs.api.openWriter(name, append)
      end,
    },
  })
  ok(handle:writeDocument("game_heartbeat", { seq = 1 }) ~= nil, "two refusals then success: the write goes through")
  equal(writerOpens, 3, "without exceeding the three-attempt budget")
  equal(fs:read(path("game_heartbeat")), '{"seq":1}', "and the bytes landed intact")

  local stuckFs = Mock.newFilesystem()
  local stuckOpens = 0
  local stuck = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openReader = function(name) return stuckFs.api.openReader(name) end,
      openWriter = function()
        stuckOpens = stuckOpens + 1
        return nil
      end,
    },
  })
  local written, reason = stuck:writeDocument("game_heartbeat", { seq = 1 })
  isNil(written, "a permanently refused open fails the write honestly")
  contains(reason, path("game_heartbeat"), "naming the path")
  contains(reason, "3 attempts", "and how many attempts were spent")
  equal(stuckOpens, 3, "with no attempts beyond the budget")
end

Harness.group("a refused reader open is retried inside the call, boundedly")
do
  local fs = Mock.newFilesystem()
  local readerOpens = 0
  local readerRefusals = 2
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openWriter = function(name, append) return fs.api.openWriter(name, append) end,
      openReader = function(name)
        readerOpens = readerOpens + 1
        if readerRefusals > 0 then
          readerRefusals = readerRefusals - 1
          return nil
        end
        return fs.api.openReader(name)
      end,
    },
  })
  fs:put(path("session"), '{"nonce":"abc"}')
  local document = handle:readDocument("session")
  equal(document.nonce, "abc", "two refused opens then success: the document reads")
  equal(readerOpens, 3, "without exceeding the three-attempt budget")

  -- A file that stays nil for the whole budget is absent, and absent keeps
  -- its meaning: the exact wording Runtime.readSession treats as silence.
  local absent, absentReason = handle:readDocument("capabilities")
  isNil(absent, "an absent file is still not a document after the retries")
  equal(absentReason, "document is empty or absent", "with the wording the retry budget must not change")
end

Harness.group("a reader close that raises is reported, not swallowed")
do
  local fs = Mock.newFilesystem()
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openWriter = function(name, append) return fs.api.openWriter(name, append) end,
      openReader = function(name)
        local reader = fs.api.openReader(name)
        if reader == nil then
          return nil
        end
        return {
          readLine = function() return reader:readLine() end,
          close = function() error("mock close failure", 0) end,
        }
      end,
    },
  })
  fs:put(path("command_queue"), '{"seq":1}\n{"seq":2}\n')
  local lines, closeFailure = handle:readLines("command_queue")
  equal(#lines, 2, "the read still returns its data")
  contains(closeFailure, "closing", "but the failed close is reported alongside it")
  contains(closeFailure, path("command_queue"), "naming the file whose handle leaked")
  contains(closeFailure, "mock close failure", "and carrying the engine's reason")
end

Harness.group("the slot cache stops the mod colliding with its own pointer")
do
  local fs = Mock.newFilesystem()
  local pointerReads = 0
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openWriter = function(name, append) return fs.api.openWriter(name, append) end,
      openReader = function(name)
        if name == path("snapshot_pointer") then
          pointerReads = pointerReads + 1
        end
        return fs.api.openReader(name)
      end,
    },
  })
  -- A healthy disk left behind by an earlier run: the pointer names slot a.
  fs:put(path("snapshot_a"), '{"seq":7}')
  fs:put(path("snapshot_pointer"), '{"slot":"a","seq":7,"written_at_ms":0}')
  equal(handle:publishSnapshot({ seq = 8 }), "b", "the first publish follows the pointer on disk")
  equal(pointerReads, 1, "which is the one and only pointer read")
  equal(handle:publishSnapshot({ seq = 9 }), "a", "the second publish alternates")
  equal(pointerReads, 1, "from memory, without opening the pointer for reading again")
  equal(handle:publishSnapshot({ seq = 10 }), "b", "and so does the third")
  equal(pointerReads, 1, "the cache holds for the life of the handle")
end

Harness.group("a refused pointer commit is carried over and committed first")
do
  local fs = Mock.newFilesystem()
  local refusePointerWrites = false
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openReader = function(name) return fs.api.openReader(name) end,
      openWriter = function(name, append)
        if refusePointerWrites and name == path("snapshot_pointer") then
          return nil
        end
        return fs.api.openWriter(name, append)
      end,
    },
  })
  equal(handle:publishSnapshot({ seq = 1 }), "a", "a healthy first publish commits slot a")

  refusePointerWrites = true
  local slot, reason = handle:publishSnapshot({ seq = 2 })
  isNil(slot, "with the pointer refused the publish does not claim success")
  contains(reason, "refused", "and says the open was refused")
  equal(Json.decode(fs:read(path("snapshot_pointer"))).slot, "a", "the disk pointer still names the committed slot")
  equal(Json.decode(fs:read(path("snapshot_pointer"))).seq, 1, "at its committed sequence")
  equal(Json.decode(fs:read(path("snapshot_b"))).seq, 2, "even though the slot write itself succeeded")

  refusePointerWrites = false
  equal(handle:publishSnapshot({ seq = 3 }), "a", "the next publish lands after the carried-over slot, not on it")
  local pointer = Json.decode(fs:read(path("snapshot_pointer")))
  equal(pointer.slot, "a", "the final pointer names the freshly published slot")
  equal(pointer.seq, 3, "with its sequence number")
  local named = Json.decode(fs:read(path(pointer.slot == "a" and "snapshot_a" or "snapshot_b")))
  equal(named.seq, 3, "and that slot holds the complete document the pointer claims")
  equal(Json.decode(fs:read(path("snapshot_b"))).seq, 2, "while the carried-over slot keeps its complete document")
end

Harness.group("a pointer refused for ten publishes is persistent, and the pending state is dropped")
do
  local fs = Mock.newFilesystem()
  local refusePointerWrites = true
  local pointerWriteOpens = 0
  local handle = Ipc.new({
    clock = function() return NOW end,
    fileApi = {
      openReader = function(name) return fs.api.openReader(name) end,
      openWriter = function(name, append)
        if name == path("snapshot_pointer") then
          pointerWriteOpens = pointerWriteOpens + 1
          if refusePointerWrites then
            return nil
          end
        end
        return fs.api.openWriter(name, append)
      end,
    },
  })
  local reasons = {}
  for publish = 1, 10 do
    local slot, reason = handle:publishSnapshot({ seq = publish })
    isNil(slot, "publish " .. publish .. " honestly fails while the pointer is refused")
    reasons[publish] = reason
  end
  contains(reasons[1], "refused", "the first failure names the refused open")
  ok(not reasons[9]:find("consecutive", 1, true), "the ninth still reports an ordinary refusal")
  contains(reasons[10], "10 consecutive publishes", "the tenth reports the persistent failure")

  -- After the drop a healthy publish starts over with a single pointer
  -- commit: no pending state survived, and the pointer names the new
  -- snapshot, never the dropped one.
  refusePointerWrites = false
  pointerWriteOpens = 0
  equal(handle:publishSnapshot({ seq = 11 }), "a", "a healthy publish after the drop succeeds")
  equal(pointerWriteOpens, 1, "with exactly one pointer commit, so nothing pending was retried")
  local pointer = Json.decode(fs:read(path("snapshot_pointer")))
  equal(pointer.seq, 11, "the pointer names the new snapshot")
  equal(Json.decode(fs:read(path("snapshot_" .. pointer.slot))).seq, 11, "whose slot holds the document it claims")
end

Harness.finish("test_ipc")
