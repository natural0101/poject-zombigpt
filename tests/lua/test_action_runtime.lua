-- Tests for PZAgent.ActionRuntime and PZAgent.CapabilityRuntime: the lifecycle
-- a command is driven through, and what the mod is willing to claim about the
-- build it is running on.
--
-- Everything here runs against spy adapters over a mock filesystem. That proves
-- the lifecycle, the gates, the budget and what lands in the ack journal. It
-- proves nothing about Project Zomboid: no timed action is created, no engine
-- symbol is called, and a build whose API differs would be caught by
-- PZAgent.CapabilityRuntime at runtime, not by this file.

local ROOT = arg[0]:match("^(.*)test_action_runtime%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Mock = dofile(ROOT .. "support/mock_game.lua")
local Support = dofile(ROOT .. "support/command_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local ActionRuntime = PZ.ActionRuntime
local CapabilityRuntime = PZ.CapabilityRuntime
local Protocol = PZ.Protocol
local REASON = Protocol.REASON
local STATUS = Protocol.STATUS
local PHASE = ActionRuntime.PHASE

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = Support.NOW
local SESSION = Support.SESSION

--- The phase names carried by the acks in the journal, in order.
local function phases(fs)
  local records = Support.acks(fs)
  local out = {}
  for index = 1, #records do
    out[index] = (records[index].message or ""):match("^phase=([a-z]+)") or "?"
  end
  return table.concat(out, ",")
end

local function lastTerminal(fs)
  local records = Support.terminalAcks(fs)
  return records[#records]
end

Harness.group("every phase maps to a status the schema declares")
do
  local statuses = {}
  for _, value in pairs(Protocol.STATUS) do
    statuses[value] = true
  end
  local mapped = 0
  for _, phase in pairs(PHASE) do
    local status = ActionRuntime.WIRE_STATUS[phase]
    ok(statuses[status] == true, phase .. " maps to a status the wire declares")
    mapped = mapped + 1
  end
  equal(mapped, 10, "all ten phases are mapped")
  equal(ActionRuntime.WIRE_STATUS[PHASE.QUEUED], STATUS.ACCEPTED, "queued is accepted")
  equal(ActionRuntime.WIRE_STATUS[PHASE.RUNNING], STATUS.STARTED, "running is started")
  equal(ActionRuntime.WIRE_STATUS[PHASE.INTERRUPTED], STATUS.CANCELLED,
    "an interruption is a cancellation carrying the reason that caused it")
  ok(ActionRuntime.TERMINAL_PHASES[PHASE.INTERRUPTED], "and it ends the command")
  isNil(ActionRuntime.TERMINAL_PHASES[PHASE.RUNNING], "while running does not")
end

Harness.group("a command runs, and every phase it passed through is on the record")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "10,20,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })

  local summary = runtime:tick(agent, NOW)
  equal(summary.admitted, 1, "one command was admitted")
  equal(spy.starts, 1, "the adapter ran once")
  equal(phases(fs), "accepted,preparing,succeeded", "the ack journal shows the whole lifecycle")

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "the terminal ack is a success")
  equal(terminal.reason_code, REASON.POSTCONDITION_MET, "carrying POSTCONDITION_MET")
  equal(terminal.evidence.position, "10,20,0", "and the evidence the adapter observed")
  equal(terminal.session_id, SESSION, "addressed to the open session")
  equal(terminal.schema_version, Protocol.SCHEMA_VERSION, "with the schema version the sidecar expects")
  equal(terminal.progress, 1.0, "and a finished progress")
end

Harness.group("an adapter that observed nothing cannot report success")
do
  local spy = Support.spyAdapter("movement.move_to")
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.POSTCONDITION_FAILED, "with POSTCONDITION_FAILED")
  contains(terminal.message, "without evidence", "and the message says what was missing")
  isNil(terminal.evidence, "no evidence was invented for it")
end

Harness.group("an adapter that raises fails its command, not the tick")
do
  local spy = Support.spyAdapter("movement.move_to", { raise = true })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  local summary = runtime:tick(agent, NOW)

  ok(summary ~= nil, "the tick completed")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "with INTERNAL_ERROR")
  contains(terminal.message, "mock adapter failure", "and the message carries what raised")
end

Harness.group("an unknown action is rejected without reaching any adapter")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "1,1,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "movement.teleport", args = {} }) })
  runtime:tick(agent, NOW)

  equal(spy.starts, 0, "the registered adapter was not called")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.REJECTED, "the command was rejected")
  equal(terminal.reason_code, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
end

Harness.group("an action with no adapter answers CAPABILITY_UNAVAILABLE")
do
  local agent, fs, runtime = Support.runtime(Mock, {})
  Support.publish(fs, { Support.command({ action = "consume.eat", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.REJECTED, "the command was rejected")
  equal(terminal.reason_code, REASON.CAPABILITY_UNAVAILABLE, "with CAPABILITY_UNAVAILABLE")
  contains(terminal.message, "consume.eat", "naming the action this build cannot perform")
  local _, reason = agent.capabilities:stateOf("consume.eat")
  equal(reason, CapabilityRuntime.REASON_NO_ADAPTER,
    "and the report keeps the true reason rather than blaming a symbol")
end

Harness.group("an adapter whose engine symbol is absent is refused before it runs")
do
  local spy = Support.spyAdapter("literature.read", {
    required_symbols = { "ISReadABook.new" },
    evidence = { reading_started = true },
  })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "literature.read", args = {} }) })
  runtime:tick(agent, NOW)

  equal(spy.starts, 0, "the adapter was never called")
  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.CAPABILITY_UNAVAILABLE, "the refusal is a capability one")
  contains(terminal.message, "ISReadABook", "and the message names the symbol that is missing")
end

Harness.group("a duplicate replays the stored result without running the adapter again")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "3,4,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  local command = Support.command({ action = "movement.move_to", args = {} })
  Support.publish(fs, { command })
  runtime:tick(agent, NOW)
  equal(spy.starts, 1, "the adapter ran once")
  local firstTerminal = lastTerminal(fs)

  Support.appendRaw(fs, Support.line(command))
  runtime:tick(agent, NOW + 100)
  equal(spy.starts, 1, "the redelivery did not run the adapter again")

  local replayed = lastTerminal(fs)
  equal(replayed.status, STATUS.SUCCEEDED, "the stored outcome was replayed")
  equal(replayed.evidence.position, "3,4,0", "with the original evidence")
  equal(replayed.command_id, command.command_id, "addressed to the redelivered command")
  ok(replayed.seq > firstTerminal.seq, "on a fresh ack sequence number")
  equal(replayed.timestamp_ms, NOW + 100, "and stamped when it was replayed")
  contains(replayed.message, "replayed", "the record says it is a replay")
end

Harness.group("only one command that takes time is in flight at once")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 3, evidence = { position = "9,9,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
  })

  runtime:tick(agent, NOW)
  equal(slow.starts, 1, "only the first command started")
  equal(runtime:pendingCount(), 1, "the second is waiting")
  local queuedAck = Support.acks(fs)[#Support.acks(fs)]
  equal(queuedAck.status, STATUS.ACCEPTED, "the waiting command is told it was accepted")
  contains(queuedAck.message, "phase=queued", "with the phase spelled out")

  runtime:tick(agent, NOW + 1)
  equal(slow.starts, 1, "it still has not started while the first runs")
  runtime:tick(agent, NOW + 2)
  equal(slow.poll_calls, 2, "the running command is stepped once per tick")
  runtime:tick(agent, NOW + 3)
  equal(slow.poll_calls, 3, "and polled until it finishes")
  equal(slow.starts, 2, "only then does the queued command start")
end

Harness.group("a full queue is told so, rather than growing")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { maxPending = 1, maxWorkPerTick = 8 })
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
  })
  runtime:tick(agent, NOW)

  equal(runtime:pendingCount(), 1, "the queue held at its cap")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.REJECTED, "the third command was rejected")
  equal(terminal.reason_code, REASON.QUEUE_REJECTED, "with QUEUE_REJECTED")
  equal(slow.starts, 1, "and only one command ever started")
end

Harness.group("safety.stop bypasses a full queue and ends the running command")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { maxPending = 1, maxWorkPerTick = 8, controls = true })
  Mock.installActionQueue({})
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "safety.stop", args = {} }),
  })
  runtime:tick(agent, NOW)

  equal(slow.interrupts, 1, "the running command's adapter was told to clean up")
  equal(runtime:pendingCount(), 0, "the queue was emptied")
  isNil(runtime:inFlight(), "and nothing is in flight")
  equal(agent.safety.armed, false, "the agent is disarmed")

  local records = Support.acks(fs)
  local stop = records[#records]
  equal(stop.action, "safety.stop", "the last ack belongs to the stop")
  equal(stop.status, STATUS.SUCCEEDED, "which succeeded")
  equal(stop.evidence.armed_before, true, "on evidence of what the agent was")
  equal(stop.evidence.armed_after, false, "and what it is now")
  equal(stop.evidence.cleared_pending, 1, "reporting the queued work it cleared")

  local interrupted, clearedFromQueue = nil, nil
  for index = 1, #records do
    local record = records[index]
    if record.action == "movement.move_to" and record.status == STATUS.CANCELLED then
      if (record.message or ""):find("phase=interrupted", 1, true) then
        interrupted = record
      else
        clearedFromQueue = record
      end
    end
  end
  ok(interrupted ~= nil, "the running command was ended as an interruption")
  equal(interrupted.reason_code, REASON.PANIC_STOP, "with PANIC_STOP")
  ok(clearedFromQueue ~= nil, "and the waiting command was ended too")
  equal(clearedFromQueue.reason_code, REASON.PANIC_STOP, "with the same reason")
  Mock.removeActionQueue()
end

Harness.group("a manual takeover interrupts the running command with its own reason")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  ok(runtime:inFlight() ~= nil, "the command is running")

  PZ.Safety.noteInput(agent.safety, "movement", NOW + 1)
  runtime:tick(agent, NOW + 2)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.CANCELLED, "the command ended")
  equal(terminal.reason_code, REASON.USER_TAKEOVER, "with USER_TAKEOVER")
  contains(terminal.message, "phase=interrupted", "as an interruption")
  equal(slow.interrupts, 1, "and the adapter was given a chance to clean up")
end

Harness.group("a lost sidecar heartbeat interrupts the running command")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {}, lease_ms = 300000 }) })
  runtime:tick(agent, NOW)

  runtime:tick(agent, NOW + PZ.Safety.SIDECAR_MAX_AGE_MS + 1)
  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.STALE_SESSION, "the reason is the stale supervisor")
  contains(terminal.message, "phase=interrupted", "reported as an interruption")
end

Harness.group("a threat interrupts the running command")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  PZ.Safety.setDanger(agent.safety, Protocol.DANGER.HIGH)
  runtime:tick(agent, NOW + 1)
  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.THREAT_INTERRUPTED, "the reason is the threat")
  equal(terminal.status, STATUS.CANCELLED, "and the command is over")
end

Harness.group("the lease is checked again immediately before the next step")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {}, issued_at_ms = NOW, lease_ms = 1000 }),
  })
  runtime:tick(agent, NOW)
  equal(slow.starts, 1, "the command was inside its lease when it started")

  runtime:tick(agent, NOW + 1001)
  equal(slow.poll_calls, 0, "the adapter was not polled against a world that moved on")
  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.LEASE_EXPIRED, "the command ended on LEASE_EXPIRED")
  contains(terminal.message, "phase=interrupted", "as an interruption of running work")
end

Harness.group("a queued command whose lease ran out while it waited is not started")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 1, evidence = { position = "0,0,0" } })
  local queued = Support.spyAdapter("inventory.transfer", { evidence = { item_ref = "x" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow, queued }, { maxWorkPerTick = 8 })
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {}, lease_ms = 300000 }),
    Support.command({ action = "inventory.transfer", args = {}, issued_at_ms = NOW, lease_ms = 1000 }),
  })
  runtime:tick(agent, NOW)
  equal(runtime:pendingCount(), 1, "a command that takes the character's queue waits its turn")
  equal(queued.starts, 0, "and has not started")

  -- The slow command finishes, and the queued one reaches the front against a
  -- world that moved on while it waited.
  runtime:tick(agent, NOW + 2000)
  equal(queued.starts, 0, "the stale command was not started")
  local terminal = lastTerminal(fs)
  equal(terminal.action, "inventory.transfer", "the last ack belongs to it")
  equal(terminal.status, STATUS.REJECTED, "it was rejected at the second check point")
  equal(terminal.reason_code, REASON.LEASE_EXPIRED, "on the lease that ran out while it waited")
end

Harness.group("work from a session that closed ends as lost")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  agent.session:terminate(REASON.SESSION_TERMINATED)
  runtime:tick(agent, NOW + 1)
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.LOST, "the command is lost")
  equal(terminal.reason_code, REASON.SESSION_TERMINATED, "and the reason names the closed session")
  equal(terminal.session_id, SESSION, "addressed to the session that issued it")
end

Harness.group("a command that is not allowed to start now is rejected with the safety reason")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "1,1,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy }, { armed = false })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  equal(spy.starts, 0, "the adapter was not reached")
  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.NOT_ARMED, "the refusal names the arming state")
end

Harness.group("the per-tick budget holds")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "2,2,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy }, { maxWorkPerTick = 2 })
  local commands = {}
  for index = 1, 6 do
    commands[index] = Support.command({ action = "movement.move_to", args = {} })
  end
  Support.publish(fs, commands)

  local summary = runtime:tick(agent, NOW)
  ok(summary.budget_exhausted, "the tick reports that it ran out of budget")
  equal(spy.starts, 1, "one command ran; the journal header spent the other unit")
  runtime:tick(agent, NOW + 1)
  equal(spy.starts, 3, "the next tick makes the same bounded progress")
  ok(spy.starts < 6, "and a long queue never drains in one tick")
end

Harness.group("the running command is stepped before anything new is admitted")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 1, evidence = { position = "5,5,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { maxWorkPerTick = 1 })
  -- No journal header, so the single unit of budget reaches the command itself.
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
  }, { header = false })
  runtime:tick(agent, NOW)
  equal(slow.starts, 1, "the first command is running")

  runtime:tick(agent, NOW + 1)
  equal(slow.poll_calls, 1, "the in-flight command got its step even with the budget spent")
  equal(lastTerminal(fs).status, STATUS.SUCCEEDED, "and finished")
  equal(slow.starts, 1, "while the second command waited its turn in the file")
end

Harness.group("plan.cancel ends the running command and clears the queue")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { controls = true, maxWorkPerTick = 8 })
  Support.publish(fs, {
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "movement.move_to", args = {} }),
    Support.command({ action = "plan.cancel", args = {} }),
  })
  runtime:tick(agent, NOW)

  isNil(runtime:inFlight(), "nothing is running")
  equal(runtime:pendingCount(), 0, "and nothing is queued")
  local records = Support.acks(fs)
  local cancel = records[#records]
  equal(cancel.action, "plan.cancel", "the cancel is the last thing acked")
  equal(cancel.status, STATUS.SUCCEEDED, "and it succeeded")
  equal(cancel.evidence.in_flight_before, true, "on evidence of what it found")
  equal(cancel.evidence.pending_after, 0, "and what it left")
  equal(agent.safety.armed, true, "cancelling is not disarming")
end

Harness.group("a targeted plan.cancel ends the named command and nothing else")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { controls = true, maxWorkPerTick = 8 })
  local first = Support.command({ action = "movement.move_to", args = {} })
  local second = Support.command({ action = "movement.move_to", args = {} })
  Support.publish(fs, {
    first,
    second,
    Support.command({ action = "plan.cancel", args = { command_id = first.command_id } }),
  })
  runtime:tick(agent, NOW)

  isNil(runtime:inFlight(), "the named in-flight command is gone")
  equal(runtime:pendingCount(), 1, "and the queued command it did not name kept its turn")

  local records = Support.acks(fs)
  local cancelAck, firstTerminal = nil, nil
  for index = 1, #records do
    local record = records[index]
    if record.action == "plan.cancel" then
      cancelAck = record
    elseif record.command_id == first.command_id and PZ.Protocol.isTerminalStatus(record.status) then
      firstTerminal = record
    end
  end
  equal(cancelAck.status, STATUS.SUCCEEDED, "the cancel succeeded")
  equal(cancelAck.evidence.cancelled_command_id, first.command_id, "naming the command it ended")
  equal(cancelAck.evidence.pending_left, 1, "and reporting the queued work it left alone")
  equal(firstTerminal.status, STATUS.CANCELLED, "the named command ended cancelled")
  equal(firstTerminal.reason_code, REASON.CANCELLED_BY_REQUEST, "by request")

  runtime:tick(agent, NOW + 1)
  equal(slow.starts, 2, "and the surviving command started on the next tick")
end

Harness.group("a targeted plan.cancel reaches a command still waiting in the queue")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { controls = true, maxWorkPerTick = 8 })
  local running = Support.command({ action = "movement.move_to", args = {} })
  local waiting = Support.command({ action = "movement.move_to", args = {} })
  Support.publish(fs, {
    running,
    waiting,
    Support.command({ action = "plan.cancel", args = { command_id = waiting.command_id } }),
  })
  runtime:tick(agent, NOW)

  ok(runtime:inFlight() ~= nil, "the running command was not touched")
  equal(runtime:inFlight().command.command_id, running.command_id, "and it is still the same one")
  equal(runtime:pendingCount(), 0, "while the named queued command is gone")

  local records = Support.acks(fs)
  local cancelAck, waitingTerminal = nil, nil
  for index = 1, #records do
    local record = records[index]
    if record.action == "plan.cancel" then
      cancelAck = record
    elseif record.command_id == waiting.command_id and PZ.Protocol.isTerminalStatus(record.status) then
      waitingTerminal = record
    end
  end
  equal(cancelAck.status, STATUS.SUCCEEDED, "the cancel succeeded")
  equal(cancelAck.evidence.cancelled_command_id, waiting.command_id, "naming the queued command")
  equal(cancelAck.evidence.pending_before, 1, "on evidence of what the queue held")
  equal(cancelAck.evidence.pending_after, 0, "and what it holds now")
  equal(waitingTerminal.status, STATUS.CANCELLED, "the queued command ended cancelled")
end

Harness.group("a targeted plan.cancel that matches nothing cancels nothing and says so")
do
  local slow = Support.spyAdapter("movement.move_to", { polls = 99, evidence = { position = "0,0,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { slow }, { controls = true, maxWorkPerTick = 8 })
  local running = Support.command({ action = "movement.move_to", args = {} })
  Support.publish(fs, {
    running,
    Support.command({
      action = "plan.cancel",
      args = { command_id = "ffffffff-ffff-4fff-8fff-ffffffffffff" },
    }),
  })
  runtime:tick(agent, NOW)

  ok(runtime:inFlight() ~= nil, "the running command was not the one named, so it kept running")
  equal(runtime:inFlight().command.command_id, running.command_id, "untouched")

  local terminal = lastTerminal(fs)
  equal(terminal.action, "plan.cancel", "the cancel itself is what ended")
  equal(terminal.status, STATUS.FAILED, "as a failure, not a success over the wrong command")
  equal(terminal.reason_code, REASON.PRECONDITION_FAILED, "because the named command is not here")
  contains(terminal.message, "no in-flight or queued command", "and the message says so")
end

Harness.group("session.arm reports the mode it observed, not the one it was asked for")
do
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true, armed = false })
  Support.publish(fs, { Support.command({ action = "session.arm", args = { mode = "ASSISTED" } }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "the agent armed")
  equal(terminal.evidence.mode_before, Protocol.MODE.OFF, "from OFF")
  equal(terminal.evidence.mode_after, Protocol.MODE.ASSISTED, "into ASSISTED")
  equal(terminal.evidence.armed_after, true, "and it is armed")
  equal(agent.safety.mode, Protocol.MODE.ASSISTED, "which the safety state confirms")
end

Harness.group("session.arm refuses a mode outside the enum before the adapter runs")
do
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true, armed = false })
  Support.publish(fs, { Support.command({ action = "session.arm", args = { mode = "GOD_MODE" } }) })
  runtime:tick(agent, NOW)

  equal(lastTerminal(fs).reason_code, REASON.INVALID_ARGUMENT, "the argument is refused")
  equal(agent.safety.armed, false, "and nothing armed")
end

Harness.group("session.disarm always works and proves it")
do
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "session.disarm", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "the disarm succeeded")
  equal(terminal.evidence.armed_before, true, "on observed before")
  equal(terminal.evidence.armed_after, false, "and after values")
  equal(agent.safety.mode, Protocol.MODE.OFF, "the mode is OFF")
end

Harness.group("action.wait refuses honestly when there is no world clock to observe")
do
  -- The wait is measured against the game's world clock, and this interpreter
  -- has neither PZAgent.Observe nor getGameTime. Real milliseconds keep
  -- passing between these ticks, and none of them may count: a wall-clock wait
  -- would report success for a wait the paused world never saw. The full
  -- behaviour against a readable clock lives in test_control_wait.lua.
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "action.wait", args = { game_seconds = 30 } }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the wait failed rather than counting real time")
  equal(terminal.reason_code, REASON.CAPABILITY_UNAVAILABLE, "as a missing capability")
  contains(terminal.message, "Observe", "naming what it could not read")
end

Harness.group("action.wait refuses the argument the wire no longer carries")
do
  -- `duration_ms` is the key the mod used to declare while the sidecar sent
  -- `game_seconds`; every wait died on it. The dispatcher must refuse it
  -- outright, so the old shape can never quietly come back.
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "action.wait", args = { duration_ms = 500 } }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.REJECTED, "the command was refused before any adapter ran")
  equal(terminal.reason_code, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  contains(terminal.message, "duration_ms", "naming the key the adapter does not declare")
end

Harness.group("world.inspect fails honestly when there is nothing to observe")
do
  local agent, fs, runtime = Support.runtime(Mock, {}, { controls = true })
  Support.publish(fs, { Support.command({ action = "world.inspect", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "with no observation module loaded the command fails")
  equal(terminal.reason_code, REASON.CAPABILITY_UNAVAILABLE, "as a missing capability")
  contains(terminal.message, "PZAgent.Observe.tick", "naming the symbol it needed")
end

Harness.group("a runtime cannot be built without its collaborators")
do
  local runtime, reason = ActionRuntime.new({})
  isNil(runtime, "a runtime with no registry is refused")
  contains(reason, "adapter registry", "and says what is missing")

  local second, secondReason = ActionRuntime.new({ dispatcher = PZ.CommandDispatcher.new() })
  isNil(second, "a runtime with no reader is refused")
  contains(secondReason, "command reader", "and says what is missing")

  local ticked, tickReason = ActionRuntime.tick({}, NOW)
  isNil(ticked, "ticking an agent with no runtime is refused")
  contains(tickReason, "no action runtime", "rather than silently doing nothing")
end

-- ---------------------------------------------------------------------------
-- capability runtime
-- ---------------------------------------------------------------------------

Harness.group("a symbol is probed in the live VM, both halves guarded")
do
  isNil(CapabilityRuntime.probeSymbol("NoSuchGlobalHere"), "an absent global does not resolve")
  local _, reason = CapabilityRuntime.probeSymbol("NoSuchGlobalHere")
  contains(reason, "not available", "and the reason names it")

  local _, malformed = CapabilityRuntime.probeSymbol("os.execute('rm')")
  contains(malformed, "plain engine symbol", "a name that is not a symbol is refused outright")

  Mock.installActionQueue({})
  ok(CapabilityRuntime.probeSymbol("ISTimedActionQueue"), "a present global resolves")
  ok(CapabilityRuntime.probeSymbol("ISTimedActionQueue.clear"), "so does a callable member")
  local _, notCallable = CapabilityRuntime.probeSymbol("ISTimedActionQueue.queue")
  contains(notCallable, "not callable", "a member that is not callable is not a usable API")
  Mock.removeActionQueue()

  local _, gone = CapabilityRuntime.probeSymbol("ISTimedActionQueue.clear")
  contains(gone, "not available", "and the verdict follows the build, not a cached answer")
end

Harness.group("the report covers every protocol action, and claims nothing it did not probe")
do
  local spy = Support.spyAdapter("movement.move_to", {
    capability = "move_to_square",
    required_symbols = { "ISTimedActionQueue" },
    evidence = { position = "1,2,0" },
  })
  local agent, _, runtime = Support.runtime(Mock, { spy })
  local capabilities = agent.capabilities
  ok(runtime ~= nil, "the runtime installed")

  local state, reason = capabilities:stateOf("consume.eat")
  equal(state, Protocol.CAPABILITY.UNSUPPORTED, "an action with no adapter is unsupported")
  equal(reason, CapabilityRuntime.REASON_NO_ADAPTER, "for want of an adapter")

  local moveState, moveReason = capabilities:stateOf("movement.move_to")
  equal(moveState, Protocol.CAPABILITY.UNSUPPORTED, "the symbol is absent in this interpreter")
  equal(moveReason, CapabilityRuntime.REASON_SYMBOL_MISSING, "so the symbol is what is reported")

  Mock.installActionQueue({})
  capabilities:probe(NOW)
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.AVAILABLE_UNVERIFIED,
    "with the symbol present the best a probe may say is available_unverified")

  local report = capabilities:report(NOW)
  equal(report.protocol_version, Protocol.PROTOCOL_VERSION, "the report states the protocol it speaks")
  equal(report.build_verified, false, "the build could not be read here, and says so")
  equal(report.actions["movement.move_to"].capability, "move_to_square",
    "the probe name from the adapter is carried")
  equal(report.capabilities["move_to_square"].state, Protocol.CAPABILITY.AVAILABLE_UNVERIFIED,
    "and the blueprint-shaped view agrees")
  local covered = 0
  for _ in pairs(report.actions) do
    covered = covered + 1
  end
  equal(covered, #Protocol.ACTION_NAMES, "every action in the protocol is covered")
  Mock.removeActionQueue()
end

Harness.group("only a succeeded ack with evidence may mint verified")
do
  local spy = Support.spyAdapter("movement.move_to", {
    required_symbols = {},
    evidence = { position = "7,7,0" },
  })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  local capabilities = agent.capabilities
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.AVAILABLE_UNVERIFIED,
    "the action starts out unverified")

  equal(capabilities:noteAck({
    action = "movement.move_to",
    status = STATUS.SUCCEEDED,
    evidence = {},
  }), false, "a success with an empty evidence bag changes nothing")
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.AVAILABLE_UNVERIFIED,
    "and the state is untouched")

  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.VERIFIED,
    "a real succeeded ack is what verifies it")

  local written = fs:read("pz_agent/" .. PZ.Ipc.FILES.capabilities)
  ok(written ~= nil, "the report was written to capabilities.json")
  local document = PZ.Json.decode(written)
  equal(document.actions["movement.move_to"].state, Protocol.CAPABILITY.VERIFIED,
    "and the file on disk carries the upgrade")
  equal(document.actions["movement.move_to"].verified_at_ms, NOW, "stamped when it was observed")
end

Harness.group("a CAPABILITY_UNAVAILABLE failure drops a capability back to unsupported")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "1,1,0" } })
  local agent = Support.runtime(Mock, { spy })
  local capabilities = agent.capabilities
  capabilities:noteAck({
    action = "movement.move_to",
    status = STATUS.SUCCEEDED,
    evidence = { position = "1,1,0" },
    timestamp_ms = NOW,
    command_id = "c1",
  })
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.VERIFIED, "verified first")

  ok(capabilities:noteAck({
    action = "movement.move_to",
    status = STATUS.FAILED,
    reason_code = REASON.CAPABILITY_UNAVAILABLE,
    message = "ISWalkToTimedAction.new is not available in this build",
  }), "the failure changes the report")
  equal(capabilities:stateOf("movement.move_to"), Protocol.CAPABILITY.UNSUPPORTED,
    "the claim is withdrawn")

  equal(capabilities:noteAck({
    action = "movement.move_to",
    status = STATUS.FAILED,
    reason_code = REASON.NO_SAFE_FOOD,
  }), false, "a failure about the world says nothing about the API")
  ok(not capabilities:isUsable("movement.move_to"), "an unsupported action is not attempted")
end

Harness.group("a capability runtime needs a registry, and an unknown action is refused")
do
  local capabilities, reason = CapabilityRuntime.new(nil)
  isNil(capabilities, "there is nothing to probe without a registry")
  contains(reason, "adapter registry", "and it says so")

  local built = CapabilityRuntime.new(PZ.CommandDispatcher.new())
  local noted, why = built:noteAck({ action = "movement.fly", status = STATUS.SUCCEEDED })
  equal(noted, false, "an ack for an action outside the protocol changes nothing")
  contains(why, "movement.fly", "and the reason names it")
end

-- ---------------------------------------------------------------------------
-- the convention the game-facing adapters use
-- ---------------------------------------------------------------------------

Harness.group("an adapter may answer in the game-facing convention")
do
  local walked = { starts = 0, polls = 0 }
  local adapter = {
    action = "movement.move_to",
    required_symbols = {},
    start = function(context, args)
      walked.starts = walked.starts + 1
      walked.args = args
      context.state.evidence = { position_before = "0,0,0" }
      -- `true`: queued and running, in the convention client/PZAgent/adapters
      -- writes.
      return true
    end,
    poll = function(context)
      walked.polls = walked.polls + 1
      context.state.evidence.position_after = "4,4,0"
      return "done"
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { adapter })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  equal(walked.starts, 1, "the adapter started")
  ok(runtime:inFlight() ~= nil, "and true was read as still running")

  runtime:tick(agent, NOW + 1)
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "\"done\" finished the command")
  equal(terminal.evidence.position_after, "4,4,0",
    "on the evidence the adapter accumulated on its own state")
end

Harness.group("a refusal that names a reason code keeps that code")
do
  local adapter = {
    action = "movement.move_to",
    required_symbols = {},
    start = function()
      return nil, REASON.TARGET_NOT_LOADED, "the target square is not loaded"
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { adapter })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.TARGET_NOT_LOADED, "with the code the adapter named")
  contains(terminal.message, "not loaded", "and its detail")
end

Harness.group("a refusal that names an interruption ends as one")
do
  local adapter = {
    action = "movement.move_to",
    required_symbols = {},
    start = function()
      return nil, REASON.USER_TAKEOVER, "the player took over"
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { adapter })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.CANCELLED, "the command was taken away, not refused")
  contains(terminal.message, "phase=interrupted", "and it is recorded as an interruption")
end

Harness.group("a refusal with a code nobody defined is an internal error")
do
  local adapter = {
    action = "movement.move_to",
    required_symbols = {},
    start = function()
      return nil, "TARGET_TOO_SPOOKY", "invented on the spot"
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { adapter })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "an unknown code never reaches the sidecar")
  contains(terminal.message, "TARGET_TOO_SPOOKY", "but what was said is still reported")
end

Harness.group("the arguments reach an adapter that reads them as a parameter")
do
  local seen = {}
  local adapter = {
    action = "movement.move_to",
    required_symbols = {},
    args = { x = { type = PZ.CommandDispatcher.ARG.NUMBER, required = true, integer = true } },
    start = function(_, args)
      seen.x = args.x
      return { done = true, evidence = { x = args.x } }
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { adapter })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = { x = 12 } }) })
  runtime:tick(agent, NOW)
  equal(seen.x, 12, "the sanitised argument arrived as the second parameter")
  equal(lastTerminal(fs).evidence.x, 12, "and the command succeeded on it")
end

Harness.group("install adopts the adapters the sibling files published")
do
  local published = Support.spyAdapter("literature.read", { evidence = { reading_started = true } })
  local clash = Support.spyAdapter("action.wait", { evidence = { waited_ms = 1 } })
  PZ.Adapters = { ALL = { published, clash } }

  local agent, fs = Support.agent(Mock)
  local runtime, reason = ActionRuntime.install(agent, NOW)
  ok(runtime ~= nil, "the runtime installed: " .. tostring(reason))
  ok(agent.dispatcher:adapterFor("literature.read") == published, "the published adapter was adopted")
  contains(agent.safety.last_error, "action.wait",
    "one that collides with a control adapter costs its own action, and is reported")

  Support.publish(fs, { Support.command({ action = "literature.read", args = {} }) })
  runtime:tick(agent, NOW)
  equal(published.starts, 1, "and a command for it reaches the adapter")
  equal(lastTerminal(fs).status, STATUS.SUCCEEDED, "which drives it to a terminal state")
  PZ.Adapters = nil
end

Harness.group("a refusal is addressed to the session that issued the command")
do
  local agent, fs, runtime = Support.runtime(Mock, {})
  Support.publish(fs, {
    Support.command({ session_id = Support.OTHER_SESSION, action = "action.wait", args = {} }),
  })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.reason_code, REASON.STALE_SESSION, "the command was refused as stale")
  equal(terminal.session_id, Support.OTHER_SESSION,
    "and answered on the session that sent it, not on the one that is open")
end

Harness.group("an evidence bag past the cap is refused, not trimmed")
do
  local wide = {}
  for index = 1, ActionRuntime.MAX_EVIDENCE_KEYS + 1 do
    wide["observed_" .. index] = index
  end
  local spy = Support.spyAdapter("movement.move_to", { evidence = wide })
  local agent, fs, runtime = Support.runtime(Mock, { spy })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command did not succeed")
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "the adapter is at fault, not the world")
  isNil(terminal.evidence, "and no trimmed half of the proof was published")
end

-- ---------------------------------------------------------------------------
-- exception hardening: a raise is a failure, never an escape
--
-- On the 2026-08-08 live run (Build 42.20.2) ActionRuntime.verify raised on a
-- Kahlua gap after session.arm; the exception escaped the event handler, the
-- terminal ack never appeared, and the runtime held its in-flight slot
-- forever. These groups pin the contract that replaced that behaviour: any
-- raise in start, poll, verify or the ack write path ends its command as
-- failed/INTERNAL_ERROR with the phase named in the detail, clears the slot,
-- and leaves the runtime able to run safety.stop and the next valid command.
-- ---------------------------------------------------------------------------

Harness.group("a raise inside verify is a terminal INTERNAL_ERROR, never a success")
do
  local spy = Support.spyAdapter("movement.move_to", { evidence = { position = "1,2,0" } })
  local good = Support.spyAdapter("inventory.transfer", { evidence = { item_ref = "x" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy, good }, { maxWorkPerTick = 8 })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })

  local realVerify = ActionRuntime.verify
  ActionRuntime.verify = function()
    error("boom", 0)
  end
  local summary = runtime:tick(agent, NOW)
  ActionRuntime.verify = realVerify

  ok(summary ~= nil, "the tick survived the raise")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed -- a raise has no route to succeeded")
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "with INTERNAL_ERROR")
  contains(terminal.message, "verify raised", "the detail names the phase that raised")
  contains(terminal.message, "boom", "and carries what it said")
  isNil(runtime:inFlight(), "the slot was cleared")

  Support.appendRaw(fs, Support.line(Support.command({ action = "inventory.transfer", args = {} })))
  runtime:tick(agent, NOW + 1)
  local following = lastTerminal(fs)
  equal(following.action, "inventory.transfer", "the next valid command was accepted")
  equal(following.status, STATUS.SUCCEEDED, "and ran to its own terminal state")
end

Harness.group("a raise inside start names the phase and frees the runtime")
do
  local raising = Support.spyAdapter("movement.move_to", { raise = true })
  local good = Support.spyAdapter("inventory.transfer", { evidence = { item_ref = "x" } })
  local agent, fs, runtime = Support.runtime(Mock, { raising, good }, { maxWorkPerTick = 8 })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)

  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "with INTERNAL_ERROR")
  contains(terminal.message, "start raised", "the detail names start")
  isNil(runtime:inFlight(), "nothing was left in flight")

  Support.appendRaw(fs, Support.line(Support.command({ action = "inventory.transfer", args = {} })))
  runtime:tick(agent, NOW + 1)
  local following = lastTerminal(fs)
  equal(following.action, "inventory.transfer", "the next valid command was accepted")
  equal(following.status, STATUS.SUCCEEDED, "and ran to terminal")
end

Harness.group("a raise inside poll names the phase and frees the runtime")
do
  local polls = 0
  local exploding = {
    action = "movement.move_to",
    required_symbols = {},
    start = function(context)
      context.state.evidence = { position_before = "0,0,0" }
      -- `true`: queued and running, the game-facing convention.
      return true
    end,
    poll = function()
      polls = polls + 1
      error("poll exploded", 0)
    end,
  }
  local good = Support.spyAdapter("inventory.transfer", { evidence = { item_ref = "x" } })
  local agent, fs, runtime = Support.runtime(Mock, { exploding, good }, { maxWorkPerTick = 8 })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  ok(runtime:inFlight() ~= nil, "the command is running")

  runtime:tick(agent, NOW + 1)
  equal(polls, 1, "the poll ran once and raised")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.FAILED, "the command failed")
  equal(terminal.reason_code, REASON.INTERNAL_ERROR, "with INTERNAL_ERROR")
  contains(terminal.message, "poll raised", "the detail names poll")
  contains(terminal.message, "poll exploded", "and carries what it said")
  isNil(runtime:inFlight(), "the slot was cleared")

  Support.appendRaw(fs, Support.line(Support.command({ action = "inventory.transfer", args = {} })))
  runtime:tick(agent, NOW + 2)
  local following = lastTerminal(fs)
  equal(following.action, "inventory.transfer", "the next valid command was accepted")
  equal(following.status, STATUS.SUCCEEDED, "and ran to terminal")
end

Harness.group("safety.stop still works after an exception-failed command")
do
  local raising = Support.spyAdapter("movement.move_to", { raise = true })
  local agent, fs, runtime = Support.runtime(Mock, { raising }, { controls = true, maxWorkPerTick = 8 })
  Mock.installActionQueue({})
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  equal(lastTerminal(fs).reason_code, REASON.INTERNAL_ERROR, "the raise failed its command")

  Support.appendRaw(fs, Support.line(Support.command({ action = "safety.stop", args = {} })))
  runtime:tick(agent, NOW + 1)
  local stop = lastTerminal(fs)
  equal(stop.action, "safety.stop", "the stop is the last thing acked")
  equal(stop.status, STATUS.SUCCEEDED, "and it succeeded")
  equal(agent.safety.armed, false, "the agent is disarmed")
  Mock.removeActionQueue()
end

Harness.group("an ack write that raises still clears the slot and surfaces the failure")
do
  local spy = Support.spyAdapter("movement.move_to", { polls = 1, evidence = { position = "8,8,0" } })
  local agent, fs, runtime = Support.runtime(Mock, { spy }, { maxWorkPerTick = 8 })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })
  runtime:tick(agent, NOW)
  ok(runtime:inFlight() ~= nil, "the command is running against a healthy journal")

  -- The journal write itself now raises: worse than refusing, and exactly the
  -- shape a wedged file handle takes on Windows. The runtime must not hang on
  -- work whose terminal ack cannot be written.
  local realAppend = agent.ipc.appendRecord
  agent.ipc.appendRecord = function()
    error("the ack journal is gone", 0)
  end
  local summary = runtime:tick(agent, NOW + 1)
  agent.ipc.appendRecord = realAppend

  ok(summary ~= nil, "the tick survived the raising journal")
  isNil(runtime:inFlight(), "the slot was cleared even without a written ack")
  contains(agent.safety.last_error, "succeeded ack raised",
    "and the failure is surfaced through safety.last_error, phase named")
  isNil(lastTerminal(fs), "no terminal ack was fabricated for it")

  Support.appendRaw(fs, Support.line(Support.command({ action = "movement.move_to", args = {} })))
  runtime:tick(agent, NOW + 2)
  runtime:tick(agent, NOW + 3)
  local following = lastTerminal(fs)
  equal(following.status, STATUS.SUCCEEDED, "the next command ran to a terminal ack once the journal healed")
end

Harness.group("the evidence emptiness checks hold with the global next removed")
do
  -- The live run proved Kahlua ships `pairs` but not the global `next`; the
  -- two emptiness checks this file's module used to make through it now go
  -- through PZAgent.Compat.hasEntries. Removing `next` for the duration of
  -- the ticks proves no hidden dependency is left anywhere a command travels
  -- -- admission, verify, the ack write, the capability note. It is restored
  -- immediately after each tick, because lua5.4's own libraries (unlike the
  -- mod code) may legitimately use it.
  local full = Support.spyAdapter("movement.move_to", { evidence = { position = "6,6,0" } })
  local empty = {
    action = "inventory.transfer",
    required_symbols = {},
    start = function()
      return { done = true, evidence = {} }
    end,
  }
  local agent, fs, runtime = Support.runtime(Mock, { full, empty }, { maxWorkPerTick = 8 })
  Support.publish(fs, { Support.command({ action = "movement.move_to", args = {} }) })

  local realNext = next
  _G.next = nil
  local okFirst, firstErr = pcall(runtime.tick, runtime, agent, NOW)
  _G.next = realNext
  ok(okFirst, "the tick ran without the global next (" .. tostring(firstErr) .. ")")
  local terminal = lastTerminal(fs)
  equal(terminal.status, STATUS.SUCCEEDED, "a populated evidence bag still reads as evidence")
  equal(terminal.evidence.position, "6,6,0", "and travels on the ack")

  Support.appendRaw(fs, Support.line(Support.command({ action = "inventory.transfer", args = {} })))
  _G.next = nil
  local okSecond, secondErr = pcall(runtime.tick, runtime, agent, NOW + 1)
  _G.next = realNext
  ok(okSecond, "the second tick ran without the global next (" .. tostring(secondErr) .. ")")
  local emptyTerminal = lastTerminal(fs)
  equal(emptyTerminal.action, "inventory.transfer", "the empty-evidence command is the one that ended")
  equal(emptyTerminal.status, STATUS.FAILED, "an empty evidence bag still fails the success gate")
  equal(emptyTerminal.reason_code, REASON.POSTCONDITION_FAILED, "as a postcondition failure")
  contains(emptyTerminal.message, "without evidence", "with the message that says so")
  isNil(emptyTerminal.evidence, "and no empty bag was attached to the ack")
end

Harness.finish("test_action_runtime")
