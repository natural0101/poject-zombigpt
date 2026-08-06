-- Ownership tests for PZAgent.Ownership.
--
-- The property that matters is one-sided: an entry the player queued must never
-- appear in a panic stop's clear list, under any input. Everything else here is
-- in service of that.

local Harness = dofile((arg[0]:match("^(.*)test_ownership%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Ownership = PZ.Ownership
local CLASS = Ownership.CLASS
local OWNERSHIP = PZ.Protocol.OWNERSHIP

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil

local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"
local OTHER_SESSION = "00000000-0000-0000-0000-000000000000"

local function modEntry(index, session)
  session = session or SESSION
  return {
    index = index,
    action_type = "ISWalkToTimedAction",
    agent_tag = Ownership.tagFor(session, "cmd-" .. index),
    agent_session = session,
  }
end

local function playerEntry(index)
  return { index = index, action_type = "ISReadABook", source = "player" }
end

local function untaggedEntry(index)
  return { index = index, action_type = "ISEatFoodAction" }
end

Harness.group("tags")
do
  equal(
    Ownership.tagFor(SESSION, "cmd-1"),
    "pz_agent:" .. SESSION .. ":cmd-1",
    "a tag names the session and the command"
  )
  isNil(Ownership.tagFor(nil, "cmd-1"), "a tag needs a session")
  isNil(Ownership.tagFor(SESSION, ""), "a tag needs a command id")
end

Harness.group("classification requires positive proof")
do
  equal(Ownership.classify(modEntry(1), SESSION), CLASS.MOD, "our own tag from this session is ours")
  equal(Ownership.classify(playerEntry(1), SESSION), CLASS.MANUAL, "an entry marked as the player's is theirs")
  equal(Ownership.classify(untaggedEntry(1), SESSION), CLASS.AMBIGUOUS, "an untagged entry is ambiguous")
  equal(
    Ownership.classify(modEntry(1, OTHER_SESSION), SESSION),
    CLASS.AMBIGUOUS,
    "a tag from another session is ambiguous"
  )
  equal(
    Ownership.classify({ agent_tag = "pz_agent:x:y" }, SESSION),
    CLASS.AMBIGUOUS,
    "a tag with no session field is ambiguous"
  )
  equal(
    Ownership.classify({ agent_tag = "somebody_else:" .. SESSION .. ":1", agent_session = SESSION }, SESSION),
    CLASS.AMBIGUOUS,
    "another mod's marker on our session id is ambiguous"
  )
  equal(Ownership.classify(nil, SESSION), CLASS.AMBIGUOUS, "an entry that could not be described is ambiguous")
  equal(Ownership.classify("not a table", SESSION), CLASS.AMBIGUOUS, "a non-table entry is ambiguous")
  equal(Ownership.classify(modEntry(1), nil), CLASS.AMBIGUOUS, "with no session, nothing can be ours")
  equal(Ownership.classify(playerEntry(1), nil), CLASS.MANUAL, "the player's entry stays theirs with no session")
end

Harness.group("queue summaries")
do
  local empty = Ownership.describe({}, SESSION)
  equal(empty.ownership, OWNERSHIP.NONE, "an empty queue is owned by nobody")
  ok(not empty.busy, "an empty queue is not busy")

  local ours = Ownership.describe({ modEntry(1), modEntry(2) }, SESSION)
  equal(ours.ownership, OWNERSHIP.MOD, "a queue entirely ours reports mod")
  ok(ours.busy, "a non-empty queue is busy")
  equal(ours.mod_owned, 2, "both entries counted as ours")
  equal(ours.foreign, 0, "nothing foreign")
  ok(not Ownership.blocksAutomation(ours), "a queue that is entirely ours does not block automation")

  local mixed = Ownership.describe({ modEntry(1), playerEntry(2) }, SESSION)
  equal(mixed.ownership, OWNERSHIP.MANUAL, "one player entry makes the queue not ours")
  equal(mixed.mod_owned, 1, "our entry is still counted")
  equal(mixed.foreign, 1, "the player's entry is foreign")
  ok(Ownership.blocksAutomation(mixed), "a queue holding the player's work blocks automation")

  local unknown = Ownership.describe({ modEntry(1), untaggedEntry(2) }, SESSION)
  equal(unknown.ownership, OWNERSHIP.AMBIGUOUS, "an unrecognised entry makes the whole queue ambiguous")
  ok(Ownership.blocksAutomation(unknown), "an ambiguous queue blocks automation")

  local bothForeign = Ownership.describe({ playerEntry(1), untaggedEntry(2) }, SESSION)
  equal(bothForeign.ownership, OWNERSHIP.AMBIGUOUS, "ambiguous dominates manual, being the less known state")

  -- "I looked and there was nothing" and "I could not look" must not be the
  -- same answer: the first permits the agent to enqueue work, the second must
  -- not.
  equal(empty.readable, true, "an empty queue was readable")
  local unreadable = Ownership.describe(nil, SESSION)
  equal(unreadable.ownership, OWNERSHIP.AMBIGUOUS, "a queue that could not be read is ambiguous, not empty")
  equal(unreadable.busy, true, "and is treated as busy")
  equal(unreadable.readable, false, "and says it could not be read")
  ok(Ownership.blocksAutomation(unreadable), "so it blocks automation")
  ok(not Ownership.blocksAutomation(empty), "while an observed-empty queue does not")
  ok(Ownership.blocksAutomation("not a description"), "a description that is not a table blocks automation")
  ok(
    Ownership.blocksAutomation({ modEntry(1) }),
    "a raw entry list handed in by mistake is not an observation, so it blocks too"
  )
  ok(
    Ownership.blocksAutomation({ ownership = OWNERSHIP.NONE, busy = false }),
    "and so does a description that never claims to have been read"
  )
end

Harness.group("a queue the reader could not fully describe is never ours")
do
  -- The queue reader is bounded, so it can hand over a list it has already cut
  -- short. It says so on the list itself; ignoring that would turn "the first
  -- entries are ours" into "the queue is ours" and authorise a wholesale clear
  -- over entries nobody looked at.
  local cut = { modEntry(1), modEntry(2) }
  cut.truncated = true
  cut.dropped = 40

  local description = Ownership.describe(cut, SESSION)
  equal(description.ownership, OWNERSHIP.AMBIGUOUS, "a truncated read is ambiguous, never mod-owned")
  equal(description.total, 42, "the entries left behind are still counted")
  equal(description.mod_owned, 2, "only the described entries can be ours")
  equal(description.foreign, 40, "and every entry left behind is foreign")
  ok(description.truncated, "the description says the read was cut short")
  ok(Ownership.blocksAutomation(description), "so nothing may be enqueued")

  local plan = Ownership.panicPlan(cut, SESSION)
  equal(plan.mod_owned, 2, "the plan still knows which entries are ours")
  equal(plan.foreign, 40, "and that the rest are not")
  ok(plan.truncated, "the plan reports the truncation")
  ok(not plan.whole_queue, "and a wholesale clear is never equivalent to it")

  local vague = { modEntry(1) }
  vague.truncated = true
  local vaguePlan = Ownership.panicPlan(vague, SESSION)
  equal(vaguePlan.foreign, 1, "a reader that dropped an unknown number counts as having dropped one")
  ok(not vaguePlan.whole_queue, "which is enough to forbid a wholesale clear")

  local nonsense = { modEntry(1) }
  nonsense.truncated = true
  nonsense.dropped = -3
  ok(not Ownership.panicPlan(nonsense, SESSION).whole_queue, "a nonsensical dropped count fails the same way")
end

Harness.group("a panic stop clears only what the mod owns")
do
  local plan = Ownership.panicPlan({ modEntry(1), playerEntry(2), modEntry(3), untaggedEntry(4) }, SESSION)
  equal(plan.mod_owned, 2, "two entries are ours")
  equal(plan.foreign, 2, "two are not")
  equal(#plan.clear, 2, "the clear list holds exactly our entries")
  equal(#plan.keep, 2, "the keep list holds the rest")
  ok(not plan.whole_queue, "a wholesale clear is not equivalent when foreign entries exist")
  for index = 1, #plan.clear do
    equal(plan.clear[index].class, CLASS.MOD, "every entry to clear is classified as the mod's")
  end
  equal(plan.clear[1].index, 1, "clear entries keep their queue position")
  equal(plan.clear[2].index, 3, "clear entries keep their queue position")
  equal(plan.keep[1].class, CLASS.MANUAL, "the player's entry is kept")
  equal(plan.keep[2].class, CLASS.AMBIGUOUS, "the unrecognised entry is kept")

  local allOurs = Ownership.panicPlan({ modEntry(1), modEntry(2) }, SESSION)
  ok(allOurs.whole_queue, "with nothing foreign, clearing everything is equivalent to the plan")

  local allTheirs = Ownership.panicPlan({ playerEntry(1), untaggedEntry(2) }, SESSION)
  equal(#allTheirs.clear, 0, "a queue with nothing of ours clears nothing")
  ok(not allTheirs.whole_queue, "and never authorises a wholesale clear")

  local noSession = Ownership.panicPlan({ modEntry(1), modEntry(2) }, nil)
  equal(#noSession.clear, 0, "without a session, a stop clears nothing at all")

  local unreadable = Ownership.panicPlan(nil, SESSION)
  equal(#unreadable.clear, 0, "an unreadable queue clears nothing")
  ok(not unreadable.whole_queue, "and never authorises a wholesale clear")

  local empty = Ownership.panicPlan({}, SESSION)
  ok(not empty.whole_queue, "an empty queue needs no clear at all")

  -- Both plans count zero mod-owned entries and mean opposite things, so the
  -- plan has to carry which of the two it is.
  equal(empty.mod_owned, unreadable.mod_owned, "an empty queue and an unreadable one plan the same counts")
  equal(empty.readable, true, "but the empty one was observed")
  equal(unreadable.readable, false, "and the unreadable one was not")
end

Harness.group("an over-long queue fails safe")
do
  local entries = {}
  for index = 1, Ownership.MAX_ENTRIES + 5 do
    entries[index] = modEntry(index)
  end
  local plan = Ownership.panicPlan(entries, SESSION)
  ok(plan.truncated, "the scan reports that it stopped early")
  equal(plan.mod_owned, Ownership.MAX_ENTRIES, "only scanned entries can be ours")
  equal(plan.foreign, 5, "the unscanned tail counts as foreign")
  ok(not plan.whole_queue, "an unscanned tail forbids a wholesale clear")
  -- The diagnostic lists are bounded by the scan, not by how long the queue
  -- happens to be: a 10,000-entry queue must not produce a 10,000-entry plan.
  -- `total` is what accounts for the unscanned tail.
  equal(#plan.clear + #plan.keep, Ownership.MAX_ENTRIES, "the plan lists stay bounded by the scan")
  equal(plan.total, Ownership.MAX_ENTRIES + 5, "but the count still accounts for every entry")
end

Harness.group("no input makes a player entry clearable")
do
  -- The one-sided property, checked across the shapes an entry can take.
  local hostile = {
    playerEntry(1),
    untaggedEntry(2),
    { index = 3, agent_tag = "pz_agent:" .. SESSION .. ":x" },
    { index = 4, agent_session = SESSION },
    { index = 5, agent_tag = 42, agent_session = SESSION },
    { index = 6, agent_tag = Ownership.tagFor(OTHER_SESSION, "c"), agent_session = OTHER_SESSION },
    { index = 7, source = "player", agent_tag = Ownership.tagFor(SESSION, "c"), agent_session = SESSION },
    "junk",
    42,
  }
  local plan = Ownership.panicPlan(hostile, SESSION)
  equal(#plan.clear, 0, "not one of these shapes is treated as the mod's")
  equal(plan.foreign, #hostile, "all of them are foreign")

  -- Entry 7 carries a valid tag *and* the player marker: the player marker wins.
  equal(Ownership.classify(hostile[7], SESSION), CLASS.MANUAL, "a player marker beats a matching tag")
end

Harness.finish("test_ownership")
