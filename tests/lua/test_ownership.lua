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

  local unreadable = Ownership.describe(nil, SESSION)
  equal(unreadable.ownership, OWNERSHIP.NONE, "a queue that could not be read reports no ownership")
  ok(Ownership.blocksAutomation("not a description"), "a description that is not a table blocks automation")
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
  equal(#plan.clear + #plan.keep, Ownership.MAX_ENTRIES + 5, "every entry is accounted for")
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
