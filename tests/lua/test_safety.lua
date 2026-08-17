-- Safety tests for PZAgent.Safety: arming, panic stop, manual takeover.
--
-- These are the assertions that stand behind the promises in AGENTS.md, so they
-- are written as the promises: off by default, stop always works, stop never
-- touches the player's queue, the player always wins.

local Harness = dofile((arg[0]:match("^(.*)test_safety%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_safety%.lua$") or "") .. "support/mock_game.lua")
local PZ = Harness.loadModules()
local Safety = PZ.Safety
local Ownership = PZ.Ownership
local Protocol = PZ.Protocol
local REASON = Protocol.REASON
local CAPABILITY = Protocol.CAPABILITY

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil
local contains = Harness.contains

local NOW = 1700000000000
local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

local function modEntry(index)
  return {
    index = index,
    action_type = "ISWalkToTimedAction",
    agent_tag = Ownership.tagFor(SESSION, "cmd-" .. index),
    agent_session = SESSION,
    pzAgentTag = Ownership.tagFor(SESSION, "cmd-" .. index),
    pzAgentSession = SESSION,
    Type = "ISWalkToTimedAction",
  }
end

local function playerAction(index)
  return { index = index, Type = "ISReadABook", pzAgentSource = "player" }
end

--- A state that is live: sidecar seen, session open, player alive.
local function liveState()
  local state = Safety.newState()
  Safety.noteSidecarHeartbeat(state, NOW)
  return state
end

--- A nil cannot be stored in an override table, so removing a field needs a
--- sentinel.
local NONE = {}

local function context(overrides)
  local base = {
    sessionId = SESSION,
    playerPresent = true,
    playerAlive = true,
    -- An observed-empty queue. Leaving this out would mean "the queue could not
    -- be read", which blocks every mutating action by design.
    queue = Ownership.describe({}, SESSION),
  }
  for key, value in pairs(overrides or {}) do
    if value == NONE then
      base[key] = nil
    else
      base[key] = value
    end
  end
  return base
end

Harness.group("the default state performs nothing")
do
  local state = Safety.newState()
  equal(state.armed, false, "a fresh state is disarmed")
  equal(state.mode, Protocol.MODE.OFF, "and its mode is OFF")
  equal(state.manual_takeover, false, "with no takeover recorded")
  equal(state.danger_level, Protocol.DANGER.NONE, "and no danger assessed")
  ok(Safety.sidecarStale(state, NOW), "a sidecar never seen counts as stale, not as fresh")

  ok(not Safety.mayStart(state, "consume.eat", NOW, context()), "a mutating action is refused out of the box")

  -- With a live sidecar and an empty queue, the only thing still standing in
  -- the way is the arming state itself.
  local supervised = liveState()
  local allowed, reason = Safety.mayStart(supervised, "consume.eat", NOW, context())
  ok(not allowed, "and it is still refused once every other precondition holds")
  equal(reason, REASON.NOT_ARMED, "because the agent is not armed")
end

Harness.group("arming is explicit and conditional")
do
  local state = liveState()
  local armed, reason = Safety.arm(state, "AUTONOMOUS", NOW, context())
  ok(armed, "arming into a mutating mode succeeds when every condition holds")
  equal(reason, REASON.POSTCONDITION_MET, "and reports the success code")
  equal(state.armed, true, "the state is armed")
  equal(state.mode, "AUTONOMOUS", "in the requested mode")

  local observe = liveState()
  ok(Safety.arm(observe, "observe", NOW, context()), "OBSERVE is a valid mode to enter")
  equal(observe.mode, Protocol.MODE.OBSERVE, "the lowercase spelling normalises")
  equal(observe.armed, false, "but entering OBSERVE does not arm: it cannot mutate anything")

  local off = liveState()
  local refusedOff, offReason = Safety.arm(off, "OFF", NOW, context())
  ok(not refusedOff, "OFF is not a mode that can be armed")
  equal(offReason, REASON.INVALID_ARGUMENT, "and the refusal says the argument is wrong")

  local unknown = liveState()
  equal(
    select(2, Safety.arm(unknown, "GODMODE", NOW, context())),
    REASON.INVALID_ARGUMENT,
    "an unknown mode is refused"
  )

  local stale = Safety.newState()
  equal(select(2, Safety.arm(stale, "AUTONOMOUS", NOW, context())), REASON.STALE_SESSION, "no sidecar, no arming")

  local takenOver = liveState()
  Safety.noteInput(takenOver, "movement", NOW)
  equal(
    select(2, Safety.arm(takenOver, "AUTONOMOUS", NOW, context())),
    REASON.USER_TAKEOVER,
    "the agent cannot arm while the player is driving"
  )

  local noPlayer = liveState()
  equal(
    select(2, Safety.arm(noPlayer, "AUTONOMOUS", NOW, context({ playerPresent = false }))),
    REASON.PRECONDITION_FAILED,
    "no character, no arming"
  )

  local noSession = liveState()
  equal(
    select(2, Safety.arm(noSession, "AUTONOMOUS", NOW, context({ sessionId = NONE }))),
    REASON.STALE_SESSION,
    "no session, no arming"
  )

  local armedThenDisarmed = liveState()
  Safety.arm(armedThenDisarmed, "AUTONOMOUS", NOW, context())
  ok(Safety.disarm(armedThenDisarmed, REASON.CANCELLED_BY_REQUEST, NOW), "disarming always succeeds")
  equal(armedThenDisarmed.armed, false, "and leaves the state disarmed")
  equal(armedThenDisarmed.mode, Protocol.MODE.OFF, "and the mode back at OFF")
end

Harness.group("what may start, and what may not")
do
  local state = liveState()
  Safety.arm(state, "AUTONOMOUS", NOW, context())

  ok(Safety.mayStart(state, "consume.eat", NOW, context()), "an armed autonomous agent may eat")
  ok(Safety.mayStart(state, "world.inspect", NOW, context()), "and may inspect")

  equal(
    select(2, Safety.mayStart(state, "world.destroy", NOW, context())),
    REASON.INVALID_ARGUMENT,
    "an action outside the whitelist is refused before anything else is considered"
  )

  local expired = { issued_at_ms = NOW - 10000, lease_ms = 1000 }
  ok(Safety.leaseExpired(expired, NOW), "an old command's lease has run out")
  ok(not Safety.leaseExpired({ issued_at_ms = NOW - 100, lease_ms = 1000 }, NOW), "a recent one has not")
  ok(Safety.leaseExpired(nil, NOW), "a command that is not a table is treated as expired")
  ok(Safety.leaseExpired({ issued_at_ms = "soon" }, NOW), "a malformed lease is treated as expired")
  equal(
    select(2, Safety.mayStart(state, "consume.eat", NOW, context({ command = expired }))),
    REASON.LEASE_EXPIRED,
    "a command whose lease expired is refused"
  )

  local busy = context({ queue = Ownership.describe({ playerAction(1) }, SESSION) })
  equal(
    select(2, Safety.mayStart(state, "consume.eat", NOW, busy)),
    REASON.PLAYER_BUSY_MANUAL_ACTION,
    "the agent waits while the queue holds work it does not own"
  )

  local ours = context({ queue = Ownership.describe({ modEntry(1) }, SESSION) })
  ok(Safety.mayStart(state, "consume.eat", NOW, ours), "a queue that is entirely ours does not block us")

  Safety.setDanger(state, Protocol.DANGER.HIGH)
  equal(
    select(2, Safety.mayStart(state, "consume.eat", NOW, context())),
    REASON.THREAT_INTERRUPTED,
    "high danger suspends new mutating work"
  )
  ok(Safety.setDanger(state, Protocol.DANGER.LOW), "the danger level can be lowered again")
  ok(not Safety.setDanger(state, "apocalyptic"), "an unknown danger level is refused")
  ok(Safety.mayStart(state, "consume.eat", NOW, context()), "low danger does not suspend work")

  local observing = liveState()
  Safety.arm(observing, "OBSERVE", NOW, context())
  ok(Safety.mayStart(observing, "world.inspect", NOW, context()), "OBSERVE permits reads")
  equal(
    select(2, Safety.mayStart(observing, "consume.eat", NOW, context())),
    REASON.NOT_ARMED,
    "OBSERVE does not permit mutation"
  )

  local offState = liveState()
  equal(
    select(2, Safety.mayStart(offState, "world.inspect", NOW, context())),
    REASON.NOT_ARMED,
    "OFF permits nothing, not even a read"
  )

  local deadSidecar = liveState()
  Safety.arm(deadSidecar, "AUTONOMOUS", NOW, context())
  local later = NOW + Safety.SIDECAR_MAX_AGE_MS + 1
  equal(
    select(2, Safety.mayStart(deadSidecar, "consume.eat", later, context())),
    REASON.STALE_SESSION,
    "no new action starts once the sidecar heartbeat is gone"
  )

  local noSession = liveState()
  Safety.arm(noSession, "AUTONOMOUS", NOW, context())
  equal(
    select(2, Safety.mayStart(noSession, "consume.eat", NOW, context({ sessionId = NONE }))),
    REASON.STALE_SESSION,
    "no session, no action"
  )

  local dead = liveState()
  Safety.arm(dead, "AUTONOMOUS", NOW, context())
  equal(
    select(2, Safety.mayStart(dead, "consume.eat", NOW, context({ playerAlive = false }))),
    REASON.PRECONDITION_FAILED,
    "a dead character can do nothing"
  )
end

Harness.group("a danger floor is a reading only while something keeps taking it")
do
  -- The floor is written in one place, at the end of a successful observation.
  -- Nothing else writes it, so a mod whose observation starts failing keeps
  -- showing this gate the last level it managed to measure -- and the level a
  -- state starts at is `none`. The gate therefore reads the age as well as the
  -- level. test_observe drives the whole failure; this group is the arithmetic.
  local state = liveState()
  Safety.arm(state, "AUTONOMOUS", NOW, context())
  ok(Safety.setDanger(state, Protocol.DANGER.NONE, NOW), "a measured floor carries the moment it was measured")
  equal(state.danger_seen_ms, NOW, "which is the clock the observation ran on")
  equal(Safety.dangerAgeMs(state, NOW + 250), 250, "so its age can be asked for")

  -- The sidecar keeps beating throughout: this is the mod that stopped
  -- observing, not the sidecar that went away, and those are separate refusals.
  local edge = NOW + Safety.DANGER_MAX_AGE_MS
  Safety.noteSidecarHeartbeat(state, edge)
  ok(Safety.mayStart(state, "consume.eat", edge, context()), "a reading inside the allowance still permits work")
  Safety.noteSidecarHeartbeat(state, edge + 1)
  local allowed, reason, detail = Safety.mayStart(state, "consume.eat", edge + 1, context())
  ok(not allowed, "one measured further back than the allowance does not")
  equal(reason, REASON.PRECONDITION_FAILED, "and the refusal names a missing precondition")
  Harness.contains(detail, "measured", "saying what is missing")
  equal(
    state.danger_level,
    Protocol.DANGER.NONE,
    "while the stale level itself is left alone, not raised to invent a threat"
  )

  ok(Safety.mayStart(state, "safety.stop", edge + 1, context()), "stopping is unaffected, as it is by everything else")
  ok(
    Safety.mayStart(state, "world.inspect", edge + 1, context()),
    "and so is the read that takes the next measurement, so the refusal cannot lock itself in"
  )

  Safety.setDanger(state, Protocol.DANGER.NONE, edge + 1)
  ok(Safety.mayStart(state, "consume.eat", edge + 1, context()), "one fresh reading ends the refusal")

  local staleHigh = liveState()
  Safety.arm(staleHigh, "AUTONOMOUS", NOW, context())
  Safety.setDanger(staleHigh, Protocol.DANGER.HIGH, NOW)
  Safety.noteSidecarHeartbeat(staleHigh, edge + 1)
  equal(
    select(2, Safety.mayStart(staleHigh, "consume.eat", edge + 1, context())),
    REASON.PRECONDITION_FAILED,
    "a level nobody re-measured is no more evidence of a threat than of calm, so the age is what it is refused on"
  )
  Safety.setDanger(staleHigh, Protocol.DANGER.HIGH, edge + 1)
  equal(
    select(2, Safety.mayStart(staleHigh, "consume.eat", edge + 1, context())),
    REASON.THREAT_INTERRUPTED,
    "and measured again it is the threat it says it is"
  )

  local unmeasured = liveState()
  Safety.arm(unmeasured, "AUTONOMOUS", NOW, context())
  isNil(unmeasured.danger_seen_ms, "a state that has never observed has no measurement behind its none")
  isNil(Safety.dangerAgeMs(unmeasured, NOW), "so there is no age to judge it by")
  Safety.setDanger(unmeasured, Protocol.DANGER.LOW)
  isNil(unmeasured.danger_seen_ms, "and a level set with no clock is not dated to now on its behalf")

  equal(
    Safety.DANGER_MAX_AGE_MS,
    6 * Safety.SIDECAR_MAX_AGE_MS,
    "the allowance is the observation cadence: six heartbeats to a snapshot (PZAgent_Main), six times the heartbeat's"
  )
end

Harness.group("stop and cancel bypass everything")
do
  -- The whole point of the always-allowed set: it must survive every condition
  -- that refuses an ordinary action.
  local hostile = Safety.newState()
  Safety.noteInput(hostile, "movement", NOW)
  Safety.setDanger(hostile, Protocol.DANGER.CRITICAL)
  local worst = context({
    sessionId = NONE,
    playerPresent = false,
    playerAlive = false,
    command = { issued_at_ms = 0, lease_ms = 100 },
    queue = Ownership.describe({ playerAction(1) }, SESSION),
  })
  for _, action in ipairs({ "safety.stop", "session.disarm", "plan.cancel" }) do
    local allowed, reason = Safety.mayStart(hostile, action, NOW, worst)
    ok(allowed, action .. " is permitted even disarmed, taken over, unsupervised and out of lease")
    equal(reason, REASON.POSTCONDITION_MET, action .. " reports the success code")
  end
end

Harness.group("panic stop always works and clears only our own entries")
do
  local state = liveState()
  Safety.arm(state, "AUTONOMOUS", NOW, context())
  local outcome = Safety.panicStop(state, { modEntry(1), playerAction(2), modEntry(3) }, SESSION, NOW)
  equal(state.armed, false, "the stop disarmed the agent")
  equal(state.mode, Protocol.MODE.OFF, "and returned the mode to OFF")
  equal(outcome.was_armed, true, "the outcome records that it had been armed")
  equal(outcome.disarmed, true, "the outcome reports the disarm")
  equal(outcome.reason_code, REASON.PANIC_STOP, "with the panic stop reason code")
  equal(outcome.plan.mod_owned, 2, "two of the three entries are ours")
  equal(outcome.plan.foreign, 1, "one is not")
  equal(#outcome.plan.clear, 2, "only ours are queued for cancellation")
  equal(state.stop_count, 1, "the stop is counted")
  equal(#state.stops, 1, "and recorded in the history")

  local disarmed = Safety.newState()
  local whileOff = Safety.panicStop(disarmed, { modEntry(1) }, SESSION, NOW)
  equal(whileOff.was_armed, false, "stopping while already disarmed is fine")
  equal(disarmed.armed, false, "and leaves the agent disarmed")
  equal(whileOff.plan.mod_owned, 1, "the plan is still computed")

  local noSession = Safety.newState()
  local sessionless = Safety.panicStop(noSession, { modEntry(1) }, nil, NOW)
  equal(#sessionless.plan.clear, 0, "with no session, a stop cancels nothing")
  equal(noSession.armed, false, "but it still disarms")

  equal(state.stops[1].mod_owned, 2, "the history records what the plan proposed, not what was cancelled")
  equal(state.stops[1].queue_readable, true, "and that the queue it planned over had been read")

  local unreadable = Safety.newState()
  local blind = Safety.panicStop(unreadable, nil, SESSION, NOW)
  equal(#blind.plan.clear, 0, "an unreadable queue cancels nothing")
  equal(unreadable.armed, false, "and the disarm happens anyway")
  equal(blind.plan.readable, false, "and the plan says the queue was never read")
  equal(unreadable.stops[1].queue_readable, false, "which the stop history records too")

  local repeated = liveState()
  for index = 1, Safety.STOP_HISTORY + 4 do
    Safety.panicStop(repeated, {}, SESSION, NOW + index)
  end
  equal(#repeated.stops, Safety.STOP_HISTORY, "the stop history is bounded")
  equal(repeated.stop_count, Safety.STOP_HISTORY + 4, "while the counter keeps the true total")
end

Harness.group("manual input takes control back")
do
  local immediate = liveState()
  Safety.arm(immediate, "AUTONOMOUS", NOW, context())
  ok(Safety.noteInput(immediate, "movement", NOW), "movement is a takeover with no debounce")
  equal(immediate.manual_takeover, true, "the state records the takeover")
  equal(immediate.takeover_reason, "movement", "and what caused it")
  equal(
    select(2, Safety.mayStart(immediate, "consume.eat", NOW, context())),
    REASON.USER_TAKEOVER,
    "and no mutating action may start"
  )
  ok(not Safety.noteInput(immediate, "movement", NOW + 1), "further input does not re-trigger")

  for _, kind in ipairs({ "attack", "aim", "sprint" }) do
    local state = liveState()
    ok(Safety.noteInput(state, kind, NOW), kind .. " is a takeover with no debounce")
  end

  local debounced = liveState()
  ok(not Safety.noteInput(debounced, "ui", NOW), "one stray click is not a takeover")
  ok(not Safety.noteInput(debounced, "ui", NOW + 100), "nor are two")
  ok(Safety.noteInput(debounced, "ui", NOW + 200), "three inside the window are")
  equal(debounced.manual_takeover, true, "and the takeover is recorded")

  local spread = liveState()
  Safety.noteInput(spread, "ui", NOW)
  Safety.noteInput(spread, "ui", NOW + 100)
  ok(
    not Safety.noteInput(spread, "ui", NOW + Safety.DEBOUNCE_WINDOW_MS + 200),
    "clicks spread past the window restart the count instead of accumulating"
  )
  equal(spread.manual_takeover, false, "so occasional UI use never stops the agent")

  local cleared = liveState()
  Safety.noteInput(cleared, "movement", NOW)
  ok(Safety.clearTakeover(cleared), "clearing reports that there had been a takeover")
  equal(cleared.manual_takeover, false, "and the flag is down")
  ok(not Safety.clearTakeover(cleared), "clearing again reports that there was nothing to clear")
end

Harness.group("the safety snapshot reports the truth")
do
  local state = liveState()
  Safety.arm(state, "ASSISTED", NOW, context())
  Safety.setDanger(state, Protocol.DANGER.MEDIUM)
  local snapshot = Safety.snapshot(state, NOW)
  equal(snapshot.armed, true, "armed")
  equal(snapshot.mode, "ASSISTED", "mode")
  equal(snapshot.danger_level, "medium", "danger level")
  equal(snapshot.manual_takeover, false, "takeover")
  equal(snapshot.sidecar_stale, false, "a sidecar seen just now is not stale")
  equal(
    Safety.snapshot(state, NOW + Safety.SIDECAR_MAX_AGE_MS + 1).sidecar_stale,
    true,
    "and is stale once the window passes"
  )
end

Harness.group("the engine boundary probes before it acts")
do
  Mock.removeActionQueue()
  local entries, capability, detail = Safety.describeQueue(Mock.newPlayer())
  isNil(entries, "with no queue API there are no entries")
  equal(capability, CAPABILITY.UNSUPPORTED, "and the capability is reported unsupported")
  Harness.contains(detail, "ISTimedActionQueue", "with a detail naming what is missing")

  isNil(Safety.describeQueue(nil), "with no player there are no entries either")

  Mock.installActionQueue({ modEntry(1), playerAction(2) })
  local read, state2 = Safety.describeQueue(Mock.newPlayer())
  equal(#read, 2, "both entries are described")
  equal(read[1].agent_session, SESSION, "our tag is carried into the description")
  equal(read[2].source, "player", "the player marker is carried into the description")
  equal(state2, CAPABILITY.AVAILABLE_UNVERIFIED, "reading worked, but no live probe has verified it")

  local description = Ownership.describe(read, SESSION)
  equal(description.mod_owned, 1, "one entry is ours")
  equal(description.foreign, 1, "one is the player's")
end

Harness.group("applying a stop never destroys the player's work")
do
  local player = Mock.newPlayer()

  -- A queue holding one of ours and one of the player's: the only cancellation
  -- the game exposes is wholesale, so the queue must be left alone entirely.
  local queue = Mock.installActionQueue({ modEntry(1), playerAction(2) })
  local mixedPlan = Ownership.panicPlan(Safety.describeQueue(player), SESSION)
  local mixed = Safety.applyStop(player, mixedPlan, SESSION)
  equal(mixed.cleared, 0, "nothing was cleared")
  equal(mixed.remaining, 1, "our entry is honestly reported as still queued")
  equal(mixed.capability, CAPABILITY.UNSUPPORTED, "and the capability is reported unsupported")
  equal(#queue.queue, 2, "the queue was not touched at all")

  -- A queue that is entirely ours may be cleared wholesale.
  local ourQueue = Mock.installActionQueue({ modEntry(1), modEntry(2) })
  local ourPlan = Ownership.panicPlan(Safety.describeQueue(player), SESSION)
  local applied = Safety.applyStop(player, ourPlan, SESSION)
  equal(applied.cleared, 2, "both of our entries were observed to be gone")
  equal(applied.remaining, 0, "nothing of ours is left")
  equal(#ourQueue.queue, 0, "and the queue really is empty")

  -- Nothing of ours queued: there is nothing to do, and saying so is honest.
  Mock.installActionQueue({ playerAction(1) })
  local nothing = Safety.applyStop(player, Ownership.panicPlan(Safety.describeQueue(player), SESSION), SESSION)
  equal(nothing.cleared, 0, "nothing of ours was queued, so nothing was cleared")
  equal(nothing.capability, CAPABILITY.VERIFIED, "which is a fact the mod can verify without the game")

  -- The API is missing entirely.
  Mock.installActionQueue({ modEntry(1) }, { noClear = true })
  local noClear = Safety.applyStop(player, Ownership.panicPlan({ modEntry(1) }, SESSION), SESSION)
  equal(noClear.cleared, 0, "with no clear API nothing is cleared")
  equal(noClear.remaining, 1, "and our entry is reported as still queued")
  Harness.contains(noClear.detail, "clear", "with a detail naming the missing API")

  -- The API is present but throws.
  Mock.installActionQueue({ modEntry(1) }, { clearFails = true })
  local failed = Safety.applyStop(player, Ownership.panicPlan({ modEntry(1) }, SESSION), SESSION)
  equal(failed.cleared, 0, "a failed clear is not reported as a success")
  equal(failed.remaining, 1, "our entry is reported as still queued")
  Harness.contains(failed.detail, "failed", "with the failure carried in the detail")

  Mock.removeActionQueue()
  local blind = Safety.applyStop(Mock.newPlayer(), Ownership.panicPlan({ modEntry(1) }, SESSION), SESSION)
  equal(blind.cleared, 0, "with no queue API at all, nothing is claimed to be cleared")
  equal(blind.capability, CAPABILITY.UNSUPPORTED, "and the capability is unsupported")
end

Harness.group("a queue that could not be read is never reported as clear")
do
  -- This is the branch the mod actually takes on a build with no queue API: the
  -- read fails, the plan counts nothing of ours, and the counts are identical
  -- to those of an observed-empty queue. Calling that "verified: nothing of
  -- mine was queued" would be a postcondition claimed without an observation.
  Mock.removeActionQueue()
  local player = Mock.newPlayer()
  local entries, capability = Safety.describeQueue(player)
  isNil(entries, "the queue could not be read")
  equal(capability, CAPABILITY.UNSUPPORTED, "which is reported as unsupported")

  local blindPlan = Ownership.panicPlan(entries, SESSION)
  equal(blindPlan.mod_owned, 0, "the plan can name nothing of ours")
  equal(blindPlan.readable, false, "but it records that it never saw the queue")

  local applied = Safety.applyStop(player, blindPlan, SESSION)
  equal(applied.cleared, 0, "nothing is claimed to have been cleared")
  equal(applied.capability, CAPABILITY.UNSUPPORTED, "and the outcome is not verified")
  Harness.contains(applied.detail, "could not be read", "with a detail saying the queue was never read")

  -- The contrast that gives the branch its meaning.
  Mock.installActionQueue({})
  local seen = Safety.applyStop(player, Ownership.panicPlan(Safety.describeQueue(player), SESSION), SESSION)
  equal(seen.capability, CAPABILITY.VERIFIED, "an observed-empty queue is verified")

  local missing = Safety.applyStop(player, nil, SESSION)
  equal(missing.capability, CAPABILITY.UNSUPPORTED, "no plan at all is not a success either")
  Harness.contains(missing.detail, "no stop plan", "and says so")
end

Harness.group("a queue longer than the mod can read is left alone")
do
  -- The scenario that makes the bound dangerous: every entry the reader looks
  -- at is ours, and the player's is past the bound. Clearing wholesale here
  -- would delete work the player queued by hand.
  local entries = {}
  for index = 1, Ownership.MAX_ENTRIES do
    entries[index] = modEntry(index)
  end
  entries[Ownership.MAX_ENTRIES + 1] = playerAction(Ownership.MAX_ENTRIES + 1)
  entries[Ownership.MAX_ENTRIES + 2] = playerAction(Ownership.MAX_ENTRIES + 2)
  local queue = Mock.installActionQueue(entries)
  local player = Mock.newPlayer()

  local described = Safety.describeQueue(player)
  equal(#described, Ownership.MAX_ENTRIES, "the read stops at the scan bound")
  equal(described.truncated, true, "and declares that it stopped early")
  equal(described.dropped, 2, "naming how many entries it never looked at")

  local plan = Ownership.panicPlan(described, SESSION)
  ok(plan.truncated, "so the plan knows the queue was not fully seen")
  ok(not plan.whole_queue, "and refuses to call a wholesale clear equivalent")

  local applied = Safety.applyStop(player, plan, SESSION)
  equal(applied.cleared, 0, "nothing was cleared")
  equal(applied.capability, CAPABILITY.UNSUPPORTED, "the outcome is unsupported")
  equal(#queue.queue, Ownership.MAX_ENTRIES + 2, "and the player's entries past the bound survive")

  local shortQueue = Mock.installActionQueue({ modEntry(1) })
  local shortRead = Safety.describeQueue(player)
  isNil(shortRead.truncated, "a queue inside the bound is not marked truncated")
  equal(Safety.applyStop(player, Ownership.panicPlan(shortRead, SESSION), SESSION).cleared, 1, "and clears normally")
  equal(#shortQueue.queue, 0, "leaving the queue empty")
end

Harness.group("a queue object the mod cannot read is refused, whatever shape it arrives in")
do
  -- Found by planting, and the reason nothing caught it is worth recording:
  -- `Mock.installActionQueue` always builds `{ queue = entries or {} }`, so no
  -- test in this tree ever handed queueObject an object of another shape. The
  -- group above ("a queue that could not be read is never reported as clear")
  -- reads as coverage of exactly this refusal and is not: it removes the API
  -- entirely, which trips the *first* refusal in queueObject and returns before
  -- the shape check is evaluated. Deleting the shape check — or the whole
  -- refusal — left all 33 Lua files and the contract suite byte-identically
  -- green.
  --
  -- Two shapes, because they fail in opposite directions.
  local player = Mock.newPlayer()

  -- A. The entry list is under another name. Without the refusal, `#queue.queue`
  -- raises. Nothing catches it: Runtime.stop calls describeQueue as its FIRST
  -- statement, before the disarm, and the panic key path is not pcall-wrapped —
  -- so the raise would take the whole engine event handler down and the agent
  -- would stay armed. AGENTS.md invariant: stop always works.
  Mock.installMalformedActionQueue({ actions = {} })
  local raised, entriesA, capabilityA, detailA = pcall(Safety.describeQueue, player)
  ok(raised, "a queue of an unexpected shape is refused, not raised through (" ..
    tostring(entriesA) .. ")")
  isNil(entriesA, "no entry list was invented for it")
  equal(capabilityA, CAPABILITY.UNSUPPORTED, "and the capability says the queue is not readable")
  contains(detailA, "readable entry list", "with the detail naming what was wrong")

  -- B. The field is present and is not a table, but answers `#`. This one
  -- raises nothing: without the refusal the mod reads zero entries, calls the
  -- queue readable, and applyStop reports VERIFIED "nothing the mod owns is
  -- queued" — an unread queue reported as an observed-empty one, which is the
  -- fabricated success this file exists to prevent.
  Mock.installMalformedActionQueue({ queue = "" })
  local entriesB, capabilityB = Safety.describeQueue(player)
  isNil(entriesB, "a non-table entry list is not an empty queue")
  equal(capabilityB, CAPABILITY.UNSUPPORTED, "it is a queue that could not be read")

  local plan = Ownership.panicPlan(entriesB, SESSION)
  equal(plan.readable, false, "and the plan records that it never saw the queue")
  local applied = Safety.applyStop(player, plan, SESSION)
  ok(applied.capability ~= CAPABILITY.VERIFIED,
    "a stop over an unreadable queue is never verified")

  Mock.removeActionQueue()
end

Harness.group("an engine that raises on the queue read is a refusal, not a crash")
do
  -- The third way the queue read can fail, and the one the shape check cannot
  -- cover: the symbol exists and calling it throws. Build 42.20 is not this
  -- mod's to control, and the module's own contract is that a read it cannot
  -- perform reports UNSUPPORTED rather than escaping.
  --
  -- Where it escapes to is the point. `Runtime.stop` calls `describeQueue` as
  -- its first statement, ahead of the disarm, and `PZAgent_Main` registers the
  -- panic key with no pcall of its own — so a raise here takes the engine event
  -- handler down and the agent stays armed. `Runtime.refresh` runs the same
  -- call every tick, so the mod would also stop heartbeating.
  --
  -- Deleting the pcall left all 33 Lua files and the contract suite green: no
  -- test in this tree had an engine that raised.
  Mock.installRaisingActionQueue("Build 42 said no")
  local player = Mock.newPlayer()

  local survived, entries, capability, detail = pcall(Safety.describeQueue, player)

  ok(survived, "the raise was converted to a refusal, not thrown at the caller (" ..
    tostring(entries) .. ")")
  isNil(entries, "no entry list was invented")
  equal(capability, CAPABILITY.UNSUPPORTED, "the queue is reported as not readable")
  contains(detail, "Build 42 said no", "and the detail carries what the engine said")

  Mock.removeActionQueue()
end

Harness.group("an undated threat reading never inherits the last one's date")
do
  -- The gate above refuses work whose danger floor is older than the allowance.
  -- That only means anything while the date on the floor belongs to the reading
  -- that produced it. `setDanger` therefore clears the date when it is not
  -- given one, rather than leaving the previous reading's date in place.
  --
  -- Without the clear, a caller that measures the level but not the moment
  -- hands the gate a fresh-looking calm taken from an old scan -- the exact
  -- shape of the failure the age check exists to catch, arriving through the
  -- setter instead of through silence.
  --
  -- Honest about reach: the only shipped caller always passes a timestamp, so
  -- today this is defence against a future one rather than a live path. It is
  -- guarded because the alternative is a rule that holds by coincidence.
  local state = liveState()
  ok(Safety.setDanger(state, Protocol.DANGER.HIGH, NOW), "a dated reading records its moment")
  equal(state.danger_seen_ms, NOW, "which is the clock it was taken on")

  ok(Safety.setDanger(state, Protocol.DANGER.NONE), "an undated reading is still accepted")
  equal(state.danger_level, Protocol.DANGER.NONE, "and it does change the level")
  isNil(state.danger_seen_ms, "but it carries no date, rather than borrowing the last one")
  isNil(Safety.dangerAgeMs(state, NOW + 10000), "so nothing can call it freshly measured")
end

Harness.finish("test_safety")
