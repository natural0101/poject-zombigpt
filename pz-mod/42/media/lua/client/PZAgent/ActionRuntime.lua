--[[
PZAgent.ActionRuntime -- one command, driven through its lifecycle, with an
acknowledgement written for every step it took.

    accepted | rejected | preparing | queued | running
            | succeeded | failed | cancelled | interrupted | lost

Those ten phases are what the mod knows about a command. The wire carries the
nine statuses of the schema, so the mapping below is fixed and explicit:
`queued` and `preparing` travel as `accepted` (blueprint: queued *is* accepted),
`running` travels as `started`, and `interrupted` travels as `cancelled` with
the reason that interrupted it. The phase itself is written into the ack's
message, so a reader that wants the finer distinction has it without the mod
inventing a field the schema does not define.

The rules this file exists to hold:

**Succeeded is a claim about the world.** An adapter reaches `succeeded` only by
returning a non-empty evidence table -- observed before/after values. An adapter
that finishes with nothing to show produces `failed` with POSTCONDITION_FAILED.
There is no other constructor for a success ack here, so "the mod said it
worked" cannot become "it worked".

**One mutating command at a time.** An adapter that may take time declares a
`poll`, and only one such command is ever in flight. The rest wait in a bounded
queue and are told they are waiting.

**Stop bypasses everything.** `safety.stop` is executed the moment it is read --
past a full queue, past a running command, past the arming state. A stop that
can be queued behind anything is not a stop.

**Nothing ends silently.** Manual takeover, a threat, a lost sidecar heartbeat,
an expired lease, a session that closed under a running command: each ends the
command with its own reason code and its own ack. The lease is re-checked
immediately before every step, not only when the command arrived, because a
command can sit behind a long action and reach the front against a world that
moved on.

**A raise is a failure, never an escape.** The 2026-08-08 live run on Build
42.20.2 showed what an escaping exception costs: `verify` raised on a Kahlua
gap after session.arm, the terminal ack never appeared, and the runtime held
its in-flight slot forever -- hung on work nobody could end. Every adapter call
and every ack write is therefore driven under pcall at this layer: whatever
raises ends its command as failed with INTERNAL_ERROR, the detail names the
phase that raised (with the captured message bounded, never embedded whole),
the slot is cleared even when the ack write itself is what raised, and
safety.stop and the next valid command still work. An exception has no route
to `succeeded` -- a raise is always a failure.

**The tick is bounded.** A fixed work budget per tick, a bounded pending queue,
a bounded number of records read per poll. A long queue costs frames, never the
game thread.

The adapters registered here are the ones that need no unverified engine API:
they drive PZAgent.Safety, PZAgent.Runtime and PZAgent.Observe, all of which
already probe the game themselves. Adapters for the timed actions (walking,
transferring, eating, reading) register into the same dispatcher and are gated
by PZAgent.CapabilityRuntime; until one is registered its action answers
CAPABILITY_UNAVAILABLE, which is the honest state of a build that cannot do it.
]]

PZAgent = PZAgent or {}

local ActionRuntime = {}
PZAgent.ActionRuntime = ActionRuntime

ActionRuntime.PHASE = {
  ACCEPTED = "accepted",
  REJECTED = "rejected",
  PREPARING = "preparing",
  QUEUED = "queued",
  RUNNING = "running",
  SUCCEEDED = "succeeded",
  FAILED = "failed",
  CANCELLED = "cancelled",
  INTERRUPTED = "interrupted",
  LOST = "lost",
}

local PHASE = ActionRuntime.PHASE

--- Phase to the status the schema allows. Several phases share a status; the
--- phase name travels in the ack message, never as an invented field.
ActionRuntime.WIRE_STATUS = {
  [PHASE.ACCEPTED] = "accepted",
  [PHASE.REJECTED] = "rejected",
  [PHASE.PREPARING] = "accepted",
  [PHASE.QUEUED] = "accepted",
  [PHASE.RUNNING] = "started",
  [PHASE.SUCCEEDED] = "succeeded",
  [PHASE.FAILED] = "failed",
  [PHASE.CANCELLED] = "cancelled",
  [PHASE.INTERRUPTED] = "cancelled",
  [PHASE.LOST] = "lost",
}

ActionRuntime.TERMINAL_PHASES = {
  [PHASE.REJECTED] = true,
  [PHASE.SUCCEEDED] = true,
  [PHASE.FAILED] = true,
  [PHASE.CANCELLED] = true,
  [PHASE.INTERRUPTED] = true,
  [PHASE.LOST] = true,
}

--- Units of work one tick may do: admitting a record, stepping the running
--- command, or dequeuing the next one. Small on purpose -- this runs on the
--- game thread between two frames.
ActionRuntime.MAX_WORK_PER_TICK = 4

--- Commands that may wait for the in-flight slot.
---
--- One. The slot is still not a queue in any general sense -- a third command
--- is answered at once with QUEUE_REJECTED naming what holds the slot -- but the
--- single waiting place is worth having, because "walk to the crate, then take
--- the beans" is the shape of almost every real plan, and refusing the second
--- half costs a full round trip through the sidecar for a sequence that was
--- always going to be two commands.
---
--- This was zero at first, on the argument that a stored command would start
--- later against a world older than the lease it was issued under. That risk is
--- real and it is exactly what `interruptionFor` answers: the lease is checked
--- again immediately before the command is promoted out of the queue, and one
--- that ran out while it waited is rejected with LEASE_EXPIRED having touched
--- nothing. The machinery that makes a waiting command safe already existed and
--- was already tested; the ceiling of zero meant nothing ever reached it.
---
--- What stays true from that argument: a waiting command is `queued`, never
--- `started`, and the ack says so, so the sidecar can always tell work that is
--- running from work that is merely stored.
ActionRuntime.MAX_PENDING = 1

--- Default ceiling on how long one command may run when its adapter declares
--- no timeout of its own. The lease usually expires first; this is the bound
--- for a command whose lease is long.
ActionRuntime.DEFAULT_TIMEOUT_MS = 60 * 1000

--- Consecutive polls a command may take while the clock stands still.
---
--- The second bound on a running command, and the one that does not depend on
--- the thing that broke. Both other bounds are readings of the same clock: the
--- lease is `issued_at_ms + lease_ms` against now, the adapter timeout is
--- `now - started_at_ms`. PZAgent_Main reads that clock from getTimestampMs and
--- answers a constant when the build does not expose it, so a clock that never
--- advances is not a thought experiment here -- and against one, neither of
--- those two can ever fire. An adapter that keeps answering "not yet" would then
--- be polled forever, holding the single in-flight slot, with the sidecar
--- waiting on a terminal ack that never comes.
---
--- Only *consecutive* polls at a clock that did not move count, and any advance
--- resets the count, so this can never shorten the window of a command whose
--- clock is working: that would take 600 polls inside one millisecond, and the
--- runtime is stepped once per game tick. When it does fire it fires as
--- ACTION_TIMEOUT with the stopped clock named in the detail -- the failure the
--- build actually has, said out loud, rather than a command that hangs.
ActionRuntime.MAX_STALLED_POLLS = 600

--- Smallest advance worth another progress ack. Without it a poll that reports
--- a slightly different fraction every tick would write the ack journal full.
ActionRuntime.PROGRESS_STEP = 0.25

--- Longest detail string carried on an ack.
ActionRuntime.MAX_DETAIL_BYTES = 200

--- Depth an evidence comparison walks before it stops. Evidence is a flat bag of
--- scalars and small records by contract; the bound is here so a deeply nested
--- or cyclic table an adapter built by mistake cannot spin the game thread.
local MAX_EVIDENCE_DEPTH = 4

--- Observed values one success may rest on. An adapter needs a handful; far
--- more than this is a bag of state rather than a postcondition, and it is
--- refused rather than truncated -- cutting an evidence table down would leave
--- a success resting on proof nobody can see. The byte size of what gets
--- through is bounded again by PZAgent.Ipc's line limit.
ActionRuntime.MAX_EVIDENCE_KEYS = 24

local Handle = {}
Handle.__index = Handle

local function truncate(text)
  local value = tostring(text)
  if #value <= ActionRuntime.MAX_DETAIL_BYTES then
    return value
  end
  return value:sub(1, ActionRuntime.MAX_DETAIL_BYTES)
end

--- True when the action occupies the character's single action queue.
--- Mirrors pz_agent_core.ipc.queue.QUEUE_OCCUPYING_ACTIONS.
function ActionRuntime.isQueueOccupying(action)
  local Protocol = PZAgent.Protocol
  return Protocol.isKnownAction(action)
    and not Protocol.isReadOnly(action)
    and not Protocol.isAlwaysAllowed(action)
    and action ~= "session.arm"
end

--- Create a runtime. Returns nil plus a reason when a collaborator is missing.
function ActionRuntime.new(options)
  options = options or {}
  if type(options.dispatcher) ~= "table" or type(options.dispatcher.resolve) ~= "function" then
    return nil, "an action runtime needs an adapter registry"
  end
  if type(options.reader) ~= "table" or type(options.reader.poll) ~= "function" then
    return nil, "an action runtime needs a command reader"
  end
  return setmetatable({
    dispatcher = options.dispatcher,
    reader = options.reader,
    capabilities = options.capabilities,
    maxWorkPerTick = options.maxWorkPerTick or ActionRuntime.MAX_WORK_PER_TICK,
    maxPending = options.maxPending or ActionRuntime.MAX_PENDING,
    defaultTimeoutMs = options.defaultTimeoutMs or ActionRuntime.DEFAULT_TIMEOUT_MS,
    maxStalledPolls = options.maxStalledPolls or ActionRuntime.MAX_STALLED_POLLS,
    pending = {},
    inflight = nil,
    acks = 0,
    completed = 0,
  }, Handle)
end

--- Commands waiting for the in-flight slot.
function Handle:pendingCount()
  return #self.pending
end

--- The command being driven right now, or nil.
function Handle:inFlight()
  return self.inflight
end

-- ---------------------------------------------------------------------------
-- acknowledgements
-- ---------------------------------------------------------------------------

local function statusFor(phase, options)
  if options.status ~= nil then
    return options.status
  end
  return ActionRuntime.WIRE_STATUS[phase]
end

--- Build and append one ack. Returns the record, or nil plus a reason.
---
--- The record carries only fields the action_result schema declares: an ack the
--- sidecar refuses to parse is an ack that never happened, and the mod would
--- look silent while it was in fact answering.
function Handle:ack(agent, work, phase, reasonCode, options)
  options = options or {}
  local Protocol = PZAgent.Protocol
  local status = statusFor(phase, options)
  if status == nil then
    return nil, string.format("phase %q has no wire status", tostring(phase))
  end
  if phase == PHASE.SUCCEEDED and reasonCode ~= Protocol.REASON.POSTCONDITION_MET then
    return nil, "a succeeded ack must carry POSTCONDITION_MET"
  end
  local seq, seqError = agent.sequence:next("ack")
  if seq == nil then
    return nil, seqError
  end
  local message = "phase=" .. phase
  if options.detail ~= nil then
    message = message .. ": " .. truncate(options.detail)
  end
  local record = {
    schema_version = Protocol.SCHEMA_VERSION,
    session_id = work.session_id,
    seq = seq,
    command_id = work.command.command_id,
    action = work.command.action,
    status = status,
    -- Non-terminal acks carry POSTCONDITION_MET without claiming anything: the
    -- protocol constrains the pairing in one direction only (a *succeeded* ack
    -- must carry it), and the status is what says where the command is.
    reason_code = reasonCode or Protocol.REASON.POSTCONDITION_MET,
    timestamp_ms = options.now_ms,
    attempt = work.attempt or 1,
    message = message,
  }
  if work.started_at_ms ~= nil then
    record.started_at_ms = work.started_at_ms
  end
  if ActionRuntime.TERMINAL_PHASES[phase] then
    record.finished_at_ms = options.now_ms
  end
  if options.progress ~= nil then
    record.progress = options.progress
  end
  if phase == PHASE.SUCCEEDED then
    record.progress = 1.0
  end
  -- Emptiness via PZAgent.Compat, not the global `next`, which the 2026-08-08
  -- live run proved Kahlua does not provide; hasEntries also answers false for
  -- a non-table, preserving the type check this line used to spell out.
  if PZAgent.Compat.hasEntries(options.evidence) then
    record.evidence = options.evidence
  end
  local written, writeError = agent.ipc:appendRecord("command_ack", record)
  if written == nil then
    return nil, writeError
  end
  self.acks = self.acks + 1
  work.phase = phase
  if ActionRuntime.TERMINAL_PHASES[phase] then
    self.completed = self.completed + 1
    local remembered, rememberError = self.reader:remember(work.command.command_id, record)
    if remembered == nil then
      agent.safety.last_error = rememberError
    end
    if self.capabilities ~= nil then
      self.capabilities:noteAck(record)
    end
  end
  return record
end

--- Re-append a remembered terminal result for a redelivered command.
---
--- The stored record is replayed as it stands apart from three fields: the ack
--- gets a fresh sequence number and timestamp so the stream stays monotonic,
--- and the command id becomes the redelivery's own. A sidecar that retried
--- under a new command id matches acks by that id, so replaying the original's
--- would leave its retry unanswered forever; the original id stays visible in
--- the message.
---
--- The session is not one of those fields, and this is only ever called for a
--- redelivery inside the session that produced the stored result. `tick`
--- enforces that before calling: a result minted by a session that has since
--- closed is refused there rather than replayed here, because rewriting its
--- session id would turn another run's observation into this run's
--- postcondition, and leaving it would address the ack at a session nobody is
--- listening on.
function Handle:replay(agent, command, stored, nowMs)
  local seq, seqError = agent.sequence:next("ack")
  if seq == nil then
    return nil, seqError
  end
  local record = {}
  for key, value in pairs(stored) do
    record[key] = value
  end
  record.seq = seq
  record.timestamp_ms = nowMs
  record.command_id = command.command_id
  record.message = string.format(
    "%s; replayed for idempotency_key=%s from command_id=%s",
    tostring(stored.message or "phase=" .. tostring(stored.status)),
    tostring(command.idempotency_key),
    tostring(stored.command_id)
  )
  local written, writeError = agent.ipc:appendRecord("command_ack", record)
  if written == nil then
    return nil, writeError
  end
  self.acks = self.acks + 1
  return record
end

--- Append one ack without letting a raise escape into the tick.
---
--- The ack path crosses sequence numbering, Json encoding and the Ipc file
--- API, and on the live 2026-08-08 run it crossed a missing Kahlua global too.
--- Any of those raising must cost one ack, never the runtime: an exception
--- that escapes mid-lifecycle takes the event handler with it, leaves the
--- in-flight slot occupied forever, and the sidecar waiting on a terminal ack
--- that will never come. Returns the record, or nil plus an honest reason,
--- whether the ack refused or raised.
local function tryAck(self, agent, work, phase, reasonCode, options)
  local ok, record, ackError = pcall(Handle.ack, self, agent, work, phase, reasonCode, options)
  if not ok then
    return nil, string.format("the %s ack raised: %s", tostring(phase), truncate(record))
  end
  return record, ackError
end

-- ---------------------------------------------------------------------------
-- lifecycle
-- ---------------------------------------------------------------------------

local function newWork(command, adapter, args, sessionId, nowMs)
  return {
    command = command,
    adapter = adapter,
    args = args,
    session_id = sessionId,
    accepted_at_ms = nowMs,
    started_at_ms = nil,
    attempt = 1,
    phase = PHASE.ACCEPTED,
    state = {},
    progress = nil,
  }
end

--- The context an adapter is handed.
---
--- `player`, `safety`, `session_id` and `command_id` are not decoration:
--- PZAgent.Adapters.Toolkit reads exactly those to stamp a timed action with
--- this session's ownership marker, to read the action queue back, and to decide
--- whether a threat or a takeover means the adapter must stop. An adapter given
--- a context without them would enqueue untagged work, and a panic stop could
--- then not tell that work from what the player queued by hand -- so it would,
--- correctly, refuse to cancel it.
local function contextFor(self, agent, work, nowMs)
  return {
    agent = agent,
    runtime = self,
    command = work.command,
    command_id = work.command.command_id,
    args = work.args,
    session_id = work.session_id,
    player = agent.player,
    safety = agent.safety,
    now_ms = nowMs,
    state = work.state,
  }
end

--- End `work` in a terminal phase, freeing the in-flight slot.
---
--- The slot is cleared even when the ack write refused or raised: a work item
--- whose terminal ack cannot be written must not keep holding the runtime,
--- and the failure is surfaced through safety.last_error -- which the next
--- heartbeat carries -- rather than by wedging on work nobody can end.
function Handle:finish(agent, work, phase, reasonCode, nowMs, options)
  options = options or {}
  options.now_ms = nowMs
  local record, ackError = tryAck(self, agent, work, phase, reasonCode, options)
  if record == nil then
    agent.safety.last_error = ackError
  end
  if self.inflight == work then
    self.inflight = nil
  end
  return record
end

--- Ask an adapter to clean up after an interruption. Best effort by contract:
--- the command is over either way, and a cleanup that raises must not take the
--- tick with it.
local function interruptAdapter(work, context, reasonCode)
  if type(work.adapter.interrupt) ~= "function" then
    return nil
  end
  local ok, err = pcall(work.adapter.interrupt, context, reasonCode, work.args)
  if not ok then
    return tostring(err)
  end
  return nil
end

local reasonCodes = nil

--- True when `value` is one of the protocol's reason codes.
---
--- Built once, from PZAgent.Protocol rather than from a copy: an adapter that
--- refuses with `nil, code, detail` is only understood if the code is a real
--- one, and a typo must land as INTERNAL_ERROR rather than travel to the
--- sidecar as a reason code it does not know.
local function isReasonCode(value)
  if reasonCodes == nil then
    reasonCodes = {}
    for _, code in pairs(PZAgent.Protocol.REASON) do
      reasonCodes[code] = true
    end
  end
  return type(value) == "string" and reasonCodes[value] == true
end

--- Reason codes that end a command as an interruption rather than a failure.
--- Mirrors pz_agent_core.actions.engine's cancelling set: these say something
--- took the command away, not that the command was wrong.
local function isCancellingReason(code)
  local reasons = PZAgent.Protocol.REASON
  return code == reasons.PANIC_STOP
    or code == reasons.USER_TAKEOVER
    or code == reasons.THREAT_INTERRUPTED
    or code == reasons.CANCELLED_BY_REQUEST
    or code == reasons.PLAYER_BUSY_MANUAL_ACTION
    or code == reasons.LEASE_EXPIRED
end

--- Normalise what an adapter returned.
---
--- Two shapes are accepted, because two families of adapter write them. The
--- control adapters in this file return an outcome table. The adapters under
--- client/PZAgent/adapters return the game-facing convention: `"done"` when the
--- postcondition is already observed (with the evidence accumulated in
--- `context.state.evidence`), `true` while the work continues, and
--- `nil, reason_code, detail` to refuse. Both are unambiguous, so supporting
--- both costs nothing and neither can be mistaken for the other.
--- Compare two observations for the "did anything actually change?" test.
---
--- Structural rather than by identity: a position is a table of three numbers
--- and an adapter reads a fresh one every time it looks, so comparing the tables
--- themselves would call every reading a change and let a character who never
--- moved report a successful move.
local function sameValue(left, right, depth)
  if left == right then
    return true
  end
  if type(left) ~= "table" or type(right) ~= "table" then
    return false
  end
  if (depth or 0) >= MAX_EVIDENCE_DEPTH then
    -- Past the bound nothing more is inspected, and "I stopped looking" must not
    -- read as "these are the same": calling them different costs a spurious
    -- POSTCONDITION_FAILED, calling them equal costs a fabricated success.
    return false
  end
  for key, value in pairs(left) do
    if not sameValue(value, right[key], (depth or 0) + 1) then
      return false
    end
  end
  for key in pairs(right) do
    if left[key] == nil then
      return false
    end
  end
  return true
end

ActionRuntime.sameValue = sameValue

--- Count the before/after observations an evidence bag carries, and how many of
--- them read the same on both sides.
---
--- Two spellings are accepted, because both are already in use and inventing a
--- third would make an adapter's evidence unreadable to the sidecar's probes:
--- `hunger_before` paired with `hunger_after`, and `position_before` paired with
--- the bare `position` -- the latter is the key
--- pz_agent_core.capabilities.probes names as the move_to confirmation.
function ActionRuntime.observedPairs(evidence)
  local found = 0
  local unchanged = 0
  if type(evidence) ~= "table" then
    return found, unchanged
  end
  for key, before in pairs(evidence) do
    if type(key) == "string" then
      local stem = key:match("^(.+)_before$")
      if stem ~= nil then
        local after = evidence[stem .. "_after"]
        if after == nil then
          after = evidence[stem]
        end
        if after ~= nil then
          found = found + 1
          if sameValue(before, after, 0) then
            unchanged = unchanged + 1
          end
        end
      end
    end
  end
  return found, unchanged
end

--- Everything the adapter recorded, from both places it may record it.
---
--- `context.state.evidence` is where a game-facing adapter accumulates its
--- observations through PZAgent.Adapters.Toolkit's `state` helper -- the
--- "before" measure it takes in start() and has no way to return until it
--- finishes. An outcome table's own evidence wins on a collision: it is the
--- later reading of the two.
local function collectEvidence(work, outcome)
  local merged = nil
  local recorded = type(work.state) == "table" and work.state.evidence or nil
  if type(recorded) == "table" then
    merged = {}
    for key, value in pairs(recorded) do
      merged[key] = value
    end
  end
  if type(outcome.evidence) == "table" then
    merged = merged or {}
    for key, value in pairs(outcome.evidence) do
      merged[key] = value
    end
  end
  return merged
end

--- Decide whether a finished command may be called `succeeded`.
---
--- Returns the evidence bag, or nil plus a reason code and detail. This is the
--- gate the whole project is built around: an adapter reports that it is *done*,
--- never that it succeeded, and `applyOutcome` has no route to PHASE.SUCCEEDED
--- except through a non-nil answer from here.
function ActionRuntime.verify(work, outcome)
  local reasons = PZAgent.Protocol.REASON
  local action = (work.command and work.command.action) or "the action"
  local evidence = collectEvidence(work, outcome)
  if not PZAgent.Compat.hasEntries(evidence) then
    -- The adapter says it finished but observed nothing -- no bag at all, or an
    -- empty one; hasEntries answers false for both, the way `evidence == nil or
    -- next(evidence) == nil` used to before the live run proved Kahlua ships no
    -- global `next`. That is exactly the fabricated success this project treats
    -- as a defect, so it is reported as the postcondition failure it is.
    return nil, reasons.POSTCONDITION_FAILED, string.format("%s reported completion without evidence", action)
  end
  local found, unchanged = ActionRuntime.observedPairs(evidence)
  if found > 0 and unchanged == found and outcome.unchanged_is_success ~= true then
    -- Every measurement the adapter bothered to take reads the same on both
    -- sides of the action. The adapter went through the motions; the world did
    -- not move, and calling that a success would be a postcondition claimed
    -- against the evidence that contradicts it.
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("%s observed no change: all %d before/after readings match", action, found)
  end
  return evidence
end

local function normaliseOutcome(work, first, second, third)
  if type(first) == "table" then
    return first
  end
  if first == "done" then
    local state = work.state or {}
    return { done = true, evidence = state.evidence, detail = second }
  end
  if first == true then
    return { done = false, progress = type(second) == "number" and second or nil }
  end
  if first == nil then
    if isReasonCode(second) then
      return { failed = true, reason_code = second, detail = third }
    end
    return {
      failed = true,
      reason_code = PZAgent.Protocol.REASON.INTERNAL_ERROR,
      detail = second or third or "the adapter returned no outcome",
    }
  end
  return {
    failed = true,
    reason_code = PZAgent.Protocol.REASON.INTERNAL_ERROR,
    detail = string.format("the adapter returned %s", tostring(first)),
  }
end

--- Apply one adapter outcome.
local function applyOutcome(self, agent, work, outcome, nowMs)
  local reasons = PZAgent.Protocol.REASON
  if type(outcome) ~= "table" then
    return self:finish(agent, work, PHASE.FAILED, reasons.INTERNAL_ERROR, nowMs, {
      detail = "the adapter returned no outcome",
    })
  end
  if outcome.cancelled == true then
    return self:finish(agent, work, PHASE.CANCELLED, outcome.reason_code or reasons.CANCELLED_BY_REQUEST, nowMs, {
      detail = outcome.detail,
      evidence = outcome.evidence,
    })
  end
  if outcome.failed == true then
    local code = outcome.reason_code or reasons.PRECONDITION_FAILED
    -- A refusal that names something which took the command away ends it as an
    -- interruption: "failed" would read as "the command was wrong", and the
    -- player pressing a movement key is not the command being wrong.
    local phase = isCancellingReason(code) and PHASE.INTERRUPTED or PHASE.FAILED
    return self:finish(agent, work, phase, code, nowMs, {
      detail = outcome.detail,
      evidence = outcome.evidence,
    })
  end
  if outcome.done == true then
    -- Verification is the runtime's job, not the adapter's. "done" only says the
    -- adapter has nothing left to do; whether that amounts to an observed
    -- postcondition is decided by ActionRuntime.verify, against the readings the
    -- adapter recorded and against each other.
    --
    -- Under pcall because verify walks tables the adapter built and, on the
    -- live 2026-08-08 run, raised on a missing Kahlua global: a raise inside
    -- the success gate must read as a failure of this command, never escape
    -- the tick, and above all never fall through to the succeeded branch.
    local verified, evidence, verifyReason, verifyDetail = pcall(ActionRuntime.verify, work, outcome)
    if not verified then
      return self:finish(agent, work, PHASE.FAILED, reasons.INTERNAL_ERROR, nowMs, {
        detail = "verify raised: " .. truncate(evidence),
      })
    end
    if evidence == nil then
      return self:finish(agent, work, PHASE.FAILED, verifyReason, nowMs, { detail = verifyDetail })
    end
    local keys = 0
    for _ in pairs(evidence) do
      keys = keys + 1
    end
    if keys > ActionRuntime.MAX_EVIDENCE_KEYS then
      return self:finish(agent, work, PHASE.FAILED, reasons.INTERNAL_ERROR, nowMs, {
        detail = string.format(
          "%s offered %d observed values, past the %d an ack may carry",
          work.command.action,
          keys,
          ActionRuntime.MAX_EVIDENCE_KEYS
        ),
      })
    end
    return self:finish(agent, work, PHASE.SUCCEEDED, reasons.POSTCONDITION_MET, nowMs, {
      detail = outcome.detail,
      evidence = evidence,
    })
  end
  if type(work.adapter.poll) ~= "function" then
    return self:finish(agent, work, PHASE.FAILED, reasons.INTERNAL_ERROR, nowMs, {
      detail = string.format("%s declares no poll but did not finish in start", work.command.action),
    })
  end
  self.inflight = work
  if work.phase ~= PHASE.RUNNING then
    work.started_at_ms = work.started_at_ms or nowMs
    local record, ackError = tryAck(self, agent, work, PHASE.RUNNING, nil, {
      now_ms = nowMs,
      progress = outcome.progress,
    })
    if record == nil then
      agent.safety.last_error = ackError
    end
    work.progress = outcome.progress or 0
    return record
  end
  local progress = outcome.progress
  if type(progress) == "number" and progress >= (work.progress or 0) + ActionRuntime.PROGRESS_STEP then
    work.progress = progress
    local record, ackError = tryAck(self, agent, work, PHASE.RUNNING, nil, {
      now_ms = nowMs,
      progress = progress,
      status = PZAgent.Protocol.STATUS.PROGRESS,
    })
    if record == nil then
      agent.safety.last_error = ackError
    end
    return record
  end
  return nil
end

--- Apply an outcome with the runtime, not the tick, owning any raise.
---
--- applyOutcome crosses ActionRuntime.verify, the control adapters that finish
--- inside it (a stop cancels work, which writes acks) and the ack path itself.
--- A raise anywhere in there used to escape `begin` and `step`, kill the event
--- handler mid-lifecycle and leave the slot held forever. It now ends the
--- command as an INTERNAL_ERROR failure naming the phase whose outcome was
--- being applied, through `finish`, which clears the slot and cannot itself
--- raise -- so the pcall chain bottoms out here instead of recursing.
local function applyProtected(self, agent, work, outcome, nowMs, phaseName)
  local ok, result = pcall(applyOutcome, self, agent, work, outcome, nowMs)
  if ok then
    return result
  end
  return self:finish(agent, work, PHASE.FAILED, PZAgent.Protocol.REASON.INTERNAL_ERROR, nowMs, {
    detail = string.format("applying the %s outcome raised: %s", phaseName, truncate(result)),
  })
end

--- Call `start` on the adapter and act on what it returns.
function Handle:begin(agent, work, nowMs)
  local record, ackError = tryAck(self, agent, work, PHASE.PREPARING, nil, { now_ms = nowMs })
  if record == nil then
    agent.safety.last_error = ackError
  end
  work.started_at_ms = nowMs
  local context = contextFor(self, agent, work, nowMs)
  -- The arguments are passed twice on purpose: on the context, which is where
  -- the control adapters read them, and as the second parameter, which is what
  -- the game-facing adapters declare. One convention had to win at the call
  -- site; neither had to lose.
  local ok, first, second, third = pcall(work.adapter.start, context, work.args)
  if not ok then
    return self:finish(agent, work, PHASE.FAILED, PZAgent.Protocol.REASON.INTERNAL_ERROR, nowMs, {
      detail = "start raised: " .. truncate(first),
    })
  end
  return applyProtected(self, agent, work, normaliseOutcome(work, first, second, third), nowMs, "start")
end

--- The reason a running command must stop now, or nil when it may continue.
---
--- The lease is checked here rather than only on arrival: this is the second
--- check point of blueprint 3.9, and it is the one that catches a command that
--- waited behind a long action.
function Handle:interruptionFor(agent, work, nowMs)
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON
  local safety = agent.safety
  if not Protocol.isAlwaysAllowed(work.command.action)
    and PZAgent.Safety.leaseExpired(work.command, nowMs)
  then
    -- The lease and the adapter's timeout are two different clocks, and the two
    -- reason codes name them: LEASE_EXPIRED is the sidecar's authority to act
    -- running out, ACTION_TIMEOUT is this adapter taking too long. Reporting a
    -- lease expiry as ACTION_TIMEOUT would tell the sidecar its adapter is slow
    -- when what actually happened is that its own grant lapsed.
    --
    -- "Did anything of ours reach the character's queue" is the other fact worth
    -- distinguishing, and the phase already carries it: a command interrupted
    -- while running ends `interrupted`, one refused before it began ends
    -- `rejected`. That does not need a second meaning bolted onto the code.
    return reasons.LEASE_EXPIRED, "the command's lease ran out while it was running"
  end
  if work.session_id ~= agent.session:id() then
    return reasons.STALE_SESSION, "the session this command belongs to is no longer open"
  end
  local allowed, reasonCode, detail = PZAgent.Safety.mayStart(safety, work.command.action, nowMs, {
    command = work.command,
    sessionId = agent.session:id(),
    playerPresent = agent.player_present,
    playerAlive = agent.player_alive,
    queue = agent.queue_description,
  })
  if not allowed then
    if reasonCode == reasons.NOT_ARMED
      and safety.last_stop_ms ~= nil
      and work.started_at_ms ~= nil
      and safety.last_stop_ms >= work.started_at_ms
    then
      -- Disarmed while this command was running. "Not armed" is true but says
      -- nothing; the stop that caused it is what the sidecar needs to see.
      return safety.last_stop_reason or reasonCode, "a stop disarmed the agent while this command ran"
    end
    return reasonCode, detail
  end
  local timeout = work.adapter.timeout_ms or self.defaultTimeoutMs
  if work.started_at_ms ~= nil and (nowMs - work.started_at_ms) > timeout then
    return reasons.ACTION_TIMEOUT,
      string.format("no result within %s ms", tostring(timeout))
  end
  -- The bound that does not read the clock. Everything above this line does,
  -- so a clock that stands still disables all of it at once; this counts the
  -- polls that happened while it stood still and ends the command on them.
  if (work.stalled_polls or 0) >= (self.maxStalledPolls or ActionRuntime.MAX_STALLED_POLLS) then
    return reasons.ACTION_TIMEOUT,
      string.format(
        "the clock did not advance past %s ms across %s polls, so nothing can time this command",
        tostring(work.polled_at_ms),
        tostring(work.stalled_polls)
      )
  end
  return nil
end

--- Drive the in-flight command one step.
function Handle:step(agent, nowMs)
  local work = self.inflight
  if work == nil then
    return nil
  end
  local reasonCode, detail = self:interruptionFor(agent, work, nowMs)
  if reasonCode ~= nil then
    local context = contextFor(self, agent, work, nowMs)
    local cleanupError = interruptAdapter(work, context, reasonCode)
    if cleanupError ~= nil then
      agent.safety.last_error = cleanupError
    end
    local phase = PHASE.INTERRUPTED
    if reasonCode == PZAgent.Protocol.REASON.ACTION_TIMEOUT then
      phase = PHASE.FAILED
    end
    return self:finish(agent, work, phase, reasonCode, nowMs, { detail = detail })
  end
  -- Counted before the poll, against the reading the previous poll saw: a
  -- clock that moved at all clears the count, so only a genuinely stopped one
  -- accumulates towards the bound in interruptionFor.
  if work.polled_at_ms ~= nil and nowMs <= work.polled_at_ms then
    work.stalled_polls = (work.stalled_polls or 0) + 1
  else
    work.stalled_polls = 0
  end
  work.polled_at_ms = nowMs
  local context = contextFor(self, agent, work, nowMs)
  local ok, first, second, third = pcall(work.adapter.poll, context, work.args)
  if not ok then
    return self:finish(agent, work, PHASE.FAILED, PZAgent.Protocol.REASON.INTERNAL_ERROR, nowMs, {
      detail = "poll raised: " .. truncate(first),
    })
  end
  return applyProtected(self, agent, work, normaliseOutcome(work, first, second, third), nowMs, "poll")
end

--- End the in-flight command, if there is one. Returns its command id, or nil.
function Handle:cancelInFlight(agent, nowMs, reasonCode, phase)
  local work = self.inflight
  if work == nil then
    return nil
  end
  local context = contextFor(self, agent, work, nowMs)
  local cleanupError = interruptAdapter(work, context, reasonCode)
  if cleanupError ~= nil then
    agent.safety.last_error = cleanupError
  end
  self:finish(agent, work, phase or PHASE.INTERRUPTED, reasonCode, nowMs, {
    detail = "ended before it finished",
  })
  return work.command.command_id
end

--- Clear the pending queue. Returns how many commands were ended.
function Handle:clearPending(agent, nowMs, reasonCode, phase)
  local cleared = 0
  while #self.pending > 0 do
    local work = table.remove(self.pending, 1)
    self:finish(agent, work, phase or PHASE.CANCELLED, reasonCode, nowMs, {
      detail = "cleared from the pending queue",
    })
    cleared = cleared + 1
  end
  return cleared
end

-- ---------------------------------------------------------------------------
-- admission
-- ---------------------------------------------------------------------------

--- Refuse `command` before any adapter is reached.
local function refuse(self, agent, command, sessionId, reasonCode, detail, nowMs)
  local work = newWork(command, nil, nil, sessionId or command.session_id, nowMs)
  return self:finish(agent, work, PHASE.REJECTED, reasonCode, nowMs, { detail = detail })
end

--- Admit one command: gate it, then queue or start it.
function Handle:admit(agent, command, nowMs)
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON
  local sessionId = agent.session:id()

  -- resolve answers with the sanitised arguments on success and with a reason
  -- code in their place on refusal; the third value is the detail either way.
  local adapter, args, detail = self.dispatcher:resolve(command, sessionId)
  if adapter == nil then
    return refuse(self, agent, command, sessionId, args, detail, nowMs)
  end
  if self.capabilities ~= nil and not self.capabilities:isUsable(command.action) then
    local state, capReason, capDetail = self.capabilities:stateOf(command.action)
    return refuse(
      self,
      agent,
      command,
      sessionId,
      reasons.CAPABILITY_UNAVAILABLE,
      string.format("%s is %s (%s: %s)", command.action, state, tostring(capReason), tostring(capDetail)),
      nowMs
    )
  end
  if command.action ~= "session.arm" then
    -- Arming is the transition every other gate presupposes: Safety.mayStart
    -- refuses a mutating action while disarmed, and the arming command is
    -- mutating, so putting it through that gate would make arming impossible.
    -- The gate for arming is Safety.arm itself, which the adapter calls and
    -- which refuses on takeover, a stale sidecar, a missing player or a closed
    -- session with its own reason codes. The lease is still checked twice --
    -- by CommandReader on arrival and by interruptionFor before every step.
    local allowed, safetyReason, safetyDetail = PZAgent.Safety.mayStart(agent.safety, command.action, nowMs, {
      command = command,
      sessionId = sessionId,
      playerPresent = agent.player_present,
      playerAlive = agent.player_alive,
      queue = agent.queue_description,
    })
    if not allowed then
      return refuse(self, agent, command, sessionId, safetyReason, safetyDetail, nowMs)
    end
  end

  local work = newWork(command, adapter, args, sessionId, nowMs)
  local record, ackError = tryAck(self, agent, work, PHASE.ACCEPTED, nil, { now_ms = nowMs })
  if record == nil then
    agent.safety.last_error = ackError
  end

  if command.action == "safety.stop" then
    -- The one command that never waits: not for the in-flight slot, not for the
    -- queue, not for the budget.
    return self:begin(agent, work, nowMs)
  end
  if type(adapter.poll) ~= "function" and not ActionRuntime.isQueueOccupying(command.action) then
    -- Finishes inside start and does not touch the character's action queue, so
    -- it never occupies the single slot. Both halves are needed: an adapter
    -- that queues a timed action and returns immediately still owns the queue
    -- until that action ends, and starting a second one would put two of the
    -- mod's actions in front of the player at once.
    return self:begin(agent, work, nowMs)
  end
  if self.inflight ~= nil then
    if #self.pending >= self.maxPending then
      self.rejected_busy = (self.rejected_busy or 0) + 1
      return self:finish(agent, work, PHASE.REJECTED, reasons.QUEUE_REJECTED, nowMs, {
        detail = string.format(
          "%s (command %s) holds the single action slot; reissue when it ends",
          tostring(self.inflight.command.action),
          tostring(self.inflight.command.command_id)
        ),
      })
    end
    self.pending[#self.pending + 1] = work
    local queued, queueAckError = tryAck(self, agent, work, PHASE.QUEUED, nil, {
      now_ms = nowMs,
      detail = string.format("waiting behind %s", self.inflight.command.action),
    })
    if queued == nil then
      agent.safety.last_error = queueAckError
    end
    return queued
  end
  return self:begin(agent, work, nowMs)
end

--- End work belonging to a session that is no longer open.
---
--- `lost` rather than `cancelled`: nobody decided to stop these commands, and
--- the sidecar that issued them is not the one holding the session now.
function Handle:reconcileSession(agent, nowMs)
  local sessionId = agent.session:id()
  local ended = 0
  local reasons = PZAgent.Protocol.REASON
  local reasonCode = sessionId == nil and reasons.SESSION_TERMINATED or reasons.STALE_SESSION
  if self.inflight ~= nil and self.inflight.session_id ~= sessionId then
    self:cancelInFlight(agent, nowMs, reasonCode, PHASE.LOST)
    ended = ended + 1
  end
  local index = 1
  while index <= #self.pending do
    local work = self.pending[index]
    if work.session_id ~= sessionId then
      table.remove(self.pending, index)
      self:finish(agent, work, PHASE.LOST, reasonCode, nowMs, {
        detail = "the session this command belongs to closed before it ran",
      })
      ended = ended + 1
    else
      index = index + 1
    end
  end
  return ended
end

-- ---------------------------------------------------------------------------
-- tick
-- ---------------------------------------------------------------------------

--- One command tick.
---
--- Order matters: work from a closed session is ended first (it can only ever
--- be wrong), then the running command is stepped -- it gets its unit before
--- anything new is admitted, so a busy queue can never starve the command that
--- is holding the slot -- then new records are read within what is left of the
--- budget.
function Handle:tick(agent, nowMs)
  local summary = {
    admitted = 0,
    replayed = 0,
    dropped = 0,
    ended = 0,
    budget_exhausted = false,
  }
  summary.ended = self:reconcileSession(agent, nowMs)
  local budget = self.maxWorkPerTick
  if self.inflight ~= nil then
    self:step(agent, nowMs)
    budget = budget - 1
  end
  if self.inflight == nil and #self.pending > 0 and budget > 0 then
    local work = table.remove(self.pending, 1)
    budget = budget - 1
    local blocked, detail = self:interruptionFor(agent, work, nowMs)
    if blocked ~= nil then
      -- The second check point of blueprint 3.9, and the one that matters: this
      -- command waited behind another and is only now reaching the front. It
      -- never started, so it is rejected rather than interrupted.
      self:finish(agent, work, PHASE.REJECTED, blocked, nowMs, { detail = detail })
    else
      self:begin(agent, work, nowMs)
    end
  end
  if budget <= 0 then
    summary.budget_exhausted = true
    return summary
  end

  local sessionId = agent.session:id()
  local entries, diagnostic, consumed = self.reader:poll(sessionId, nowMs, budget)
  if entries == nil then
    agent.safety.last_error = diagnostic
    return summary
  end
  if diagnostic ~= nil then
    agent.safety.last_error = diagnostic
  end
  -- Lines, not entries: a header or a rotation marker costs a decode and
  -- produces nothing, and the budget is on the decoding.
  if (consumed or 0) >= budget then
    summary.budget_exhausted = true
  end
  local KIND = PZAgent.CommandReader.KIND
  for index = 1, #entries do
    local entry = entries[index]
    if entry.kind == KIND.COMMAND then
      -- Under pcall for the reason every adapter call already is, and for one
      -- more: admission crosses the registry, the capability report and the
      -- safety gate, none of which is an adapter and any of which can raise on
      -- a Kahlua gap or a malformed declaration. A raise there took the tick
      -- with it and left the command with no ack at all -- and the reader has
      -- already remembered it, so its redelivery answers "the original has not
      -- reached a terminal state yet" forever. Every ack write inside `admit`
      -- is itself protected and none of them runs before the parts that can
      -- raise, so the rejection below cannot be a second ack for this command.
      local admitted, admitError = pcall(Handle.admit, self, agent, entry.command, nowMs)
      if not admitted then
        local detail = "admitting the command raised: " .. truncate(admitError)
        agent.safety.last_error = detail
        refuse(self, agent, entry.command, sessionId, PZAgent.Protocol.REASON.INTERNAL_ERROR, detail, nowMs)
      end
      summary.admitted = summary.admitted + 1
    elseif entry.kind == KIND.REPLAY then
      if entry.ack.session_id ~= entry.command.session_id then
        -- The remembered result was minted by a session that has since closed:
        -- the reader's cache outlives a session, so a sidecar that restarts and
        -- reissues an idempotency key it used before meets its predecessor's
        -- answer. Replaying it would claim a postcondition nothing observed in
        -- this session, on evidence about objects whose runtime ids no longer
        -- denote the same things -- and it would carry the closed session's id,
        -- which the live sidecar drops as an ack for a foreign session, leaving
        -- the command unanswered on top of everything else. Refused, terminally,
        -- addressed to the session that is actually waiting.
        refuse(
          self,
          agent,
          entry.command,
          entry.command.session_id,
          PZAgent.Protocol.REASON.STALE_SESSION,
          -- Session first: the detail is bounded at MAX_DETAIL_BYTES and an
          -- idempotency key may be 128 of them, so the id that explains the
          -- refusal must not be the part that gets truncated away.
          string.format(
            "session %s already answered idempotency_key %s and has since closed;"
              .. " that result is not evidence about this one",
            tostring(entry.ack.session_id),
            tostring(entry.command.idempotency_key)
          ),
          nowMs
        )
      else
        -- Under pcall for the same reason every other ack write is: a replay
        -- crosses the same encode-and-append path, and a raise there must cost
        -- one replayed ack, not the whole tick.
        local replayOk, record, replayError = pcall(Handle.replay, self, agent, entry.command, entry.ack, nowMs)
        if not replayOk then
          record, replayError = nil, "the replay ack raised: " .. truncate(record)
        end
        if record == nil then
          agent.safety.last_error = replayError
        end
      end
      summary.replayed = summary.replayed + 1
    elseif entry.kind == KIND.REJECTED then
      -- Addressed to the session that issued the command, falling back to the
      -- open one. A refusal for a session neither side can name has nobody to
      -- go to, and an ack the sidecar cannot parse is worse than none.
      local addressee = entry.session_id or sessionId
      if addressee == nil then
        agent.safety.last_error = entry.detail
        summary.dropped = summary.dropped + 1
      else
        local work = newWork({
          command_id = entry.command_id,
          action = entry.action,
        }, nil, nil, addressee, nowMs)
        self:finish(agent, work, PHASE.REJECTED, entry.reason_code, nowMs, { detail = entry.detail })
        summary.ended = summary.ended + 1
      end
    else
      -- Dropped and duplicate records have nobody to answer: one carries no
      -- usable command id, the other's original ack is still coming.
      agent.safety.last_error = entry.detail
      summary.dropped = summary.dropped + 1
    end
  end
  if self.capabilities ~= nil and self.capabilities:needsPublish() then
    local published, publishError = self.capabilities:publish(agent.ipc, nowMs)
    if published == nil then
      agent.safety.last_error = publishError
    else
      agent.capability_revision = self.capabilities.revision
    end
  end
  return summary
end

--- One command tick for the wired agent. Returns nil plus a reason when no
--- runtime was installed, so a wiring mistake is visible instead of silent.
function ActionRuntime.tick(agent, nowMs)
  if type(agent) ~= "table" or type(agent.actions) ~= "table" then
    return nil, "no action runtime is installed on this agent"
  end
  return agent.actions:tick(agent, nowMs)
end

-- ---------------------------------------------------------------------------
-- control adapters
--
-- These drive the mod's own state and the modules that already probe the game
-- for themselves. None of them guesses an engine API, and each one reports the
-- postcondition it actually observed.
-- ---------------------------------------------------------------------------

local function safetySnapshotOf(safety)
  return { armed = safety.armed == true, mode = safety.mode }
end

local ArmAdapter = {
  action = "session.arm",
  required_symbols = {},
  start = function(context)
    local Protocol = PZAgent.Protocol
    local safety = context.agent.safety
    local before = safetySnapshotOf(safety)
    local requested = Protocol.normaliseMode(context.args.mode)
    local ok, reasonCode, detail = PZAgent.Safety.arm(safety, context.args.mode, context.now_ms, {
      sessionId = context.session_id,
      playerPresent = context.agent.player_present,
    })
    if not ok then
      return { failed = true, reason_code = reasonCode, detail = detail }
    end
    local after = safetySnapshotOf(safety)
    if after.mode ~= requested then
      return {
        failed = true,
        reason_code = Protocol.REASON.POSTCONDITION_FAILED,
        detail = string.format("mode is %s after arming into %s", tostring(after.mode), tostring(requested)),
        evidence = { mode_before = before.mode, mode_after = after.mode },
      }
    end
    return {
      done = true,
      -- Re-arming into the mode already held changes nothing and is still
      -- correct; the mode check above is what proves the postcondition, so the
      -- runtime's "did anything move?" test is told not to insist here.
      unchanged_is_success = true,
      evidence = {
        armed_before = before.armed,
        armed_after = after.armed,
        mode_before = before.mode,
        mode_after = after.mode,
      },
    }
  end,
}

local DisarmAdapter = {
  action = "session.disarm",
  required_symbols = {},
  start = function(context)
    local Protocol = PZAgent.Protocol
    local safety = context.agent.safety
    local before = safetySnapshotOf(safety)
    PZAgent.Safety.disarm(safety, Protocol.REASON.CANCELLED_BY_REQUEST, context.now_ms)
    local after = safetySnapshotOf(safety)
    if after.armed ~= false or after.mode ~= Protocol.MODE.OFF then
      return {
        failed = true,
        reason_code = Protocol.REASON.POSTCONDITION_FAILED,
        detail = "the agent is still armed after disarming",
        evidence = { armed_after = after.armed, mode_after = after.mode },
      }
    end
    return {
      done = true,
      -- Disarming an agent that was already disarmed is the state asked for.
      unchanged_is_success = true,
      evidence = {
        armed_before = before.armed,
        armed_after = after.armed,
        mode_before = before.mode,
        mode_after = after.mode,
      },
    }
  end,
}

local StopAdapter = {
  action = "safety.stop",
  required_symbols = {},
  start = function(context)
    local Protocol = PZAgent.Protocol
    local agent = context.agent
    local before = safetySnapshotOf(agent.safety)
    local cancelled = context.runtime:cancelInFlight(agent, context.now_ms, Protocol.REASON.PANIC_STOP)
    local clearedPending = context.runtime:clearPending(agent, context.now_ms, Protocol.REASON.PANIC_STOP)
    local outcome = PZAgent.Runtime.stop(agent, context.now_ms, Protocol.REASON.PANIC_STOP)
    local after = safetySnapshotOf(agent.safety)
    if after.armed ~= false then
      return {
        failed = true,
        reason_code = Protocol.REASON.POSTCONDITION_FAILED,
        detail = "the agent is still armed after a stop",
        evidence = { armed_after = after.armed },
      }
    end
    local applied = outcome.applied or {}
    return {
      done = true,
      -- A stop over an agent that was already disarmed reads the same on both
      -- sides and is exactly the outcome the command asked for. The armed check
      -- above is the postcondition; the readings below are the report.
      unchanged_is_success = true,
      evidence = {
        armed_before = before.armed,
        armed_after = after.armed,
        cancelled_command_id = cancelled,
        cleared_pending = clearedPending,
        -- What the stop did to the character's own action queue, as observed by
        -- re-reading it. `queue_readable` is what stops "nothing was cancelled"
        -- from being read as "nothing of ours was queued".
        queue_readable = outcome.plan.readable == true,
        mod_owned = outcome.plan.mod_owned,
        cleared = applied.cleared,
        remaining = applied.remaining,
      },
    }
  end,
}

--- Cancel the one pending command whose id is `target`. Returns its id, or nil
--- when nothing in the queue carries it. Bounded by MAX_PENDING, so the scan is
--- never long.
local function cancelPendingById(runtime, agent, nowMs, target)
  for index = 1, #runtime.pending do
    local work = runtime.pending[index]
    if work.command.command_id == target then
      table.remove(runtime.pending, index)
      runtime:finish(agent, work, ActionRuntime.PHASE.CANCELLED, PZAgent.Protocol.REASON.CANCELLED_BY_REQUEST, nowMs, {
        detail = "cancelled by request while it waited",
      })
      return work.command.command_id
    end
  end
  return nil
end

--- One targeted cancel: end the named command and nothing else.
---
--- The match is against this runtime's own record of what it is driving --
--- never against an observation, because observations do not carry an action
--- id and a field that never arrives must not be what a cancel depends on. A
--- target that matches neither the in-flight command nor a queued one gets the
--- truthful answer -- there is no such command here -- rather than a cancel of
--- whatever happened to be running.
local function cancelOne(context, target)
  local Protocol = PZAgent.Protocol
  local runtime = context.runtime
  local inflight = runtime:inFlight()
  local inflightId = inflight ~= nil and inflight.command.command_id or nil
  if inflightId == target then
    local cancelled = runtime:cancelInFlight(
      context.agent,
      context.now_ms,
      Protocol.REASON.CANCELLED_BY_REQUEST,
      ActionRuntime.PHASE.CANCELLED
    )
    return {
      done = true,
      evidence = {
        target_command_id = target,
        cancelled_command_id = cancelled,
        in_flight_before = true,
        in_flight_after = runtime:inFlight() ~= nil,
        -- Deliberately untouched: a targeted cancel aims at one command, and a
        -- queued one it did not name keeps its turn.
        pending_left = runtime:pendingCount(),
      },
    }
  end
  local pendingBefore = runtime:pendingCount()
  local cancelled = cancelPendingById(runtime, context.agent, context.now_ms, target)
  if cancelled ~= nil then
    return {
      done = true,
      evidence = {
        target_command_id = target,
        cancelled_command_id = cancelled,
        pending_before = pendingBefore,
        pending_after = runtime:pendingCount(),
      },
    }
  end
  return {
    failed = true,
    reason_code = Protocol.REASON.PRECONDITION_FAILED,
    detail = string.format("no in-flight or queued command has id %s", tostring(target)),
    evidence = {
      target_command_id = target,
      in_flight_command_id = inflightId,
      pending = pendingBefore,
    },
  }
end

local CancelAdapter = {
  action = "plan.cancel",
  required_symbols = {},
  start = function(context)
    local Protocol = PZAgent.Protocol
    local runtime = context.runtime
    if context.args.command_id ~= nil then
      return cancelOne(context, context.args.command_id)
    end
    local pendingBefore = runtime:pendingCount()
    local inFlightBefore = runtime:inFlight() ~= nil
    local cancelled = runtime:cancelInFlight(
      context.agent,
      context.now_ms,
      Protocol.REASON.CANCELLED_BY_REQUEST,
      ActionRuntime.PHASE.CANCELLED
    )
    local cleared = runtime:clearPending(context.agent, context.now_ms, Protocol.REASON.CANCELLED_BY_REQUEST)
    local pendingAfter = runtime:pendingCount()
    if pendingAfter ~= 0 or runtime:inFlight() ~= nil then
      return {
        failed = true,
        reason_code = Protocol.REASON.POSTCONDITION_FAILED,
        detail = "work is still queued after a cancel",
        evidence = { pending_after = pendingAfter },
      }
    end
    return {
      done = true,
      -- Cancelling when nothing was running already is the requested end state.
      unchanged_is_success = true,
      evidence = {
        in_flight_before = inFlightBefore,
        in_flight_after = false,
        cancelled_command_id = cancelled,
        pending_before = pendingBefore,
        pending_after = pendingAfter,
        cleared = cleared,
      },
    }
  end,
}

--- One reading of the in-game clock, or nil plus the reason there is none.
---
--- Read through PZAgent.Observe.gameFields, which already probes getGameTime
--- and formats the reading with ObserveModel.worldTime -- the very string
--- observations carry as `game.world_time` and the sidecar verifies a wait
--- against. Reading it the same way is what keeps the two halves of the wire
--- measuring the same clock.
local function readWorldClock()
  local Observe = PZAgent.Observe
  if type(Observe) ~= "table" or type(Observe.gameFields) ~= "function" then
    return nil, "PZAgent.Observe.gameFields is not loaded in this build"
  end
  local fields = Observe.gameFields()
  local text = type(fields) == "table" and fields.world_time or nil
  if type(text) ~= "string" then
    return nil, "the game did not report a readable world clock"
  end
  local year, month, day, hour, minute = text:match("^(%d+)%-(%d+)%-(%d+)T(%d+):(%d+)$")
  if year == nil then
    return nil, string.format("the world clock reading %q is not a timestamp", text)
  end
  return {
    text = text,
    date = string.format("%s-%s-%s", year, month, day),
    minute_of_day = tonumber(hour) * 60 + tonumber(minute),
  }
end

--- Game seconds between two clock readings, or nil when unmeasurable.
---
--- A clock that went backwards is a reload, not a wait, and is reported as
--- unmeasurable rather than as zero progress so it can never accumulate
--- towards success -- the same rule the sidecar's WaitAdapter applies. The
--- worldTime string is fixed-width, so string order is time order.
---
--- Across a date change the count assumes exactly one midnight passed. That is
--- a lower bound -- more midnights mean more elapsed time, never less -- so a
--- wait can finish late against a clock that jumped, but never early. A wait is
--- bounded at one in-game hour, which crosses at most one midnight on its own.
local function elapsedGameSeconds(before, after)
  if after.text == before.text then
    return 0
  end
  if after.text < before.text then
    return nil
  end
  if after.date == before.date then
    return (after.minute_of_day - before.minute_of_day) * 60
  end
  return ((1440 - before.minute_of_day) + after.minute_of_day) * 60
end

-- Declared before it is filled in: `start` calls `poll`, and a name is only in
-- scope for a closure written after the local exists.
--
-- The wait is measured in *game* seconds against the world clock, never in
-- real milliseconds against the tick clock: real time passes while the game is
-- paused or in a menu, and a wall-clock wait would report success for a wait
-- that never happened in the world the player is in. `timeout_ms` stays a
-- real-time bound -- it is what ends a wait whose world clock is paused or
-- crawling, as ACTION_TIMEOUT rather than as an invented success.
local WaitAdapter = {}
WaitAdapter.action = "action.wait"
WaitAdapter.required_symbols = {}
WaitAdapter.timeout_ms = 300000
WaitAdapter.start = function(context)
  local Protocol = PZAgent.Protocol
  local requested = context.args.game_seconds
  if requested <= 0 then
    -- The declaration's min is inclusive and zero must stay declared-valid for
    -- the range to read honestly, so the exclusive bound lives here.
    return {
      failed = true,
      reason_code = Protocol.REASON.INVALID_ARGUMENT,
      detail = "game_seconds must be greater than zero",
    }
  end
  local reading, reason = readWorldClock()
  if reading == nil then
    return {
      failed = true,
      reason_code = Protocol.REASON.CAPABILITY_UNAVAILABLE,
      detail = reason,
    }
  end
  context.state.requested_game_seconds = requested
  context.state.clock_before = reading
  return WaitAdapter.poll(context)
end
WaitAdapter.poll = function(context)
  local requested = context.state.requested_game_seconds
  local reading, reason = readWorldClock()
  if reading == nil then
    -- The clock stopped being readable mid-wait. Without it there is no way to
    -- observe the postcondition, and waiting on without one would end in a
    -- timeout that blames the duration rather than the missing clock.
    return {
      failed = true,
      reason_code = PZAgent.Protocol.REASON.CAPABILITY_UNAVAILABLE,
      detail = reason,
    }
  end
  local before = context.state.clock_before
  local elapsed = elapsedGameSeconds(before, reading)
  if elapsed ~= nil and elapsed >= requested then
    return {
      done = true,
      -- The world clock is the only thing a wait can observe, and it is what
      -- the postcondition is about: game time passed.
      evidence = {
        world_time_before = before.text,
        world_time_after = reading.text,
        elapsed_game_seconds = elapsed,
        requested_game_seconds = requested,
      },
    }
  end
  local progress = nil
  if elapsed ~= nil then
    progress = elapsed / requested
    if progress > 1 then
      progress = 1
    end
  end
  return { done = false, progress = progress }
end

local InspectAdapter = {
  action = "world.inspect",
  required_symbols = {},
  start = function(context)
    local Protocol = PZAgent.Protocol
    if type(PZAgent.Observe) ~= "table" or type(PZAgent.Observe.tick) ~= "function" then
      return {
        failed = true,
        reason_code = Protocol.REASON.CAPABILITY_UNAVAILABLE,
        detail = "PZAgent.Observe.tick is not loaded in this build",
      }
    end
    local document, reason = PZAgent.Observe.tick(context.agent, context.now_ms)
    if document == nil then
      return {
        failed = true,
        reason_code = Protocol.REASON.POSTCONDITION_FAILED,
        detail = reason or "no snapshot was published",
      }
    end
    return {
      done = true,
      evidence = {
        observation_seq = document.seq,
        timestamp_ms = document.timestamp_ms,
        full = document.full == true,
      },
    }
  end,
}

--- The actions no published adapter may take over.
---
--- These four drive this runtime's own state rather than the world, and the
--- built-in implementations are the ones that reach it: a stop served by a file
--- in adapters/ could cancel the character's queued work without ending the
--- in-flight command here, which is a stop in name only. Everything else --
--- action.wait, world.inspect -- is fair game for a published adapter, because a
--- file that reads the game reads it better than this one can.
---
--- This table was referenced below before it existed. The lookup ran against a
--- nil global, so `install` raised on any build where adapters/ had published
--- anything at all -- which is every real build.
local RUNTIME_OWNED = {
  ["session.arm"] = true,
  ["session.disarm"] = true,
  ["safety.stop"] = true,
  ["plan.cancel"] = true,
}

--- Published so the declaration dumper the contract suite runs can mirror the
--- precedence `install` applies, instead of keeping a copy of this table that
--- would drift.
ActionRuntime.RUNTIME_OWNED = RUNTIME_OWNED

--- The adapters this file ships, in registration order.
---
--- The argument declarations are filled in here rather than at load time: the
--- game loads this directory in name order, so PZAgent.CommandDispatcher does
--- not exist yet while this file is being read.
function ActionRuntime.controlAdapters()
  local ARG = PZAgent.CommandDispatcher.ARG
  ArmAdapter.args = {
    mode = {
      type = ARG.ENUM,
      required = true,
      -- OFF is missing on purpose: Safety.arm refuses it, and disarming has its
      -- own command.
      values = {
        OBSERVE = true,
        ASSISTED = true,
        AUTONOMOUS = true,
        REFLEX_ONLY = true,
        EXPERIMENTAL_INPUT = true,
      },
    },
  }
  WaitAdapter.args = {
    game_seconds = {
      type = ARG.NUMBER,
      required = true,
      -- Inclusive by the checker's nature; the adapter itself refuses zero.
      min = 0,
      -- One in-game hour, the sidecar's MAX_WAIT_GAME_SECONDS: a longer wait is
      -- a plan step, not a single action.
      max = 3600,
    },
  }
  CancelAdapter.args = {
    command_id = {
      type = ARG.STRING,
      required = false,
      -- A command id is a UUID, and a UUID is 36 bytes.
      max_bytes = 36,
    },
  }
  return { ArmAdapter, DisarmAdapter, StopAdapter, CancelAdapter, WaitAdapter, InspectAdapter }
end

--- Build the command path onto `agent` and probe what this build can do.
---
--- Returns the runtime, or nil plus a reason. Everything it builds is left on
--- the agent: `commands` (reader), `dispatcher`, `capabilities`, `actions`.
function ActionRuntime.install(agent, nowMs, options)
  options = options or {}
  if type(agent) ~= "table" or type(agent.ipc) ~= "table" then
    return nil, "installing the action runtime needs an agent with an Ipc handle"
  end
  local dispatcher = options.dispatcher or PZAgent.CommandDispatcher.new()
  if options.dispatcher == nil then
    -- The game-facing adapters publish themselves into PZAgent.Adapters.ALL as
    -- the engine loads client/PZAgent/adapters.
    local published = PZAgent.Adapters and PZAgent.Adapters.ALL
    local claimed = {}
    if type(published) == "table" then
      for index = 1, #published do
        local adapter = published[index]
        if type(adapter) == "table" and type(adapter.action) == "string" then
          claimed[adapter.action] = true
        end
      end
    end
    local adapters = ActionRuntime.controlAdapters()
    for index = 1, #adapters do
      local adapter = adapters[index]
      -- A published adapter wins for anything that touches the world: it reads
      -- the game, and the control adapter here is the fallback for a build
      -- where adapters/ did not load at all. It never wins for the control
      -- plane, because those four drive this runtime's own state -- a stop that
      -- did not cancel the in-flight command would be a stop in name only.
      if claimed[adapter.action] and not RUNTIME_OWNED[adapter.action] then
        agent.safety.last_error = string.format(
          "%s is served by a published adapter; the built-in one stands down",
          adapter.action
        )
      else
        local registered, registerError = dispatcher:register(adapter)
        if registered == nil then
          return nil, registerError
        end
      end
    end
    if type(published) == "table" then
      for index = 1, #published do
        -- A published adapter the registry refuses costs its own action -- which
        -- then answers CAPABILITY_UNAVAILABLE, the honest state of an action
        -- this build cannot serve -- rather than the whole command path.
        local registered, registerError = dispatcher:register(published[index])
        if registered == nil then
          agent.safety.last_error = registerError
        end
      end
    end
  end
  local reader, readerError = PZAgent.CommandReader.new({ ipc = agent.ipc })
  if reader == nil then
    return nil, readerError
  end
  local capabilities, capabilityError = PZAgent.CapabilityRuntime.new(dispatcher)
  if capabilities == nil then
    return nil, capabilityError
  end
  capabilities:probe(nowMs)
  local runtime, runtimeError = ActionRuntime.new({
    dispatcher = dispatcher,
    reader = reader,
    capabilities = capabilities,
    maxWorkPerTick = options.maxWorkPerTick,
    maxPending = options.maxPending,
  })
  if runtime == nil then
    return nil, runtimeError
  end
  agent.dispatcher = dispatcher
  agent.commands = reader
  agent.capabilities = capabilities
  agent.actions = runtime
  local published, publishError = capabilities:publish(agent.ipc, nowMs)
  if published == nil then
    -- The runtime is usable without the report on disk; the sidecar simply has
    -- to keep treating every capability as unverified. Reported, not fatal.
    agent.safety.last_error = publishError
  else
    agent.capability_revision = capabilities.revision
  end
  return runtime
end

return ActionRuntime
