--[[
PZAgent.Ownership -- who queued each entry in the character's action queue.

Invariant: an entry is treated as the mod's only on positive proof -- a tag this
session stamped on it when it enqueued the action. Everything else is the
player's. A missing tag, an unparsable entry, a tag from a previous session and
an entry the queue reader could not describe all collapse to "ambiguous", and
ambiguous is never cleared.

That asymmetry is the whole point (AGENTS.md, "User input always wins"). Being
wrong in the direction of "this is mine" means a panic stop deletes work the
player queued by hand; being wrong in the direction of "this is the player's"
means a stop leaves one of our own actions running, which the disarm that
accompanies every stop already prevents from being followed by another.

This module is pure. It is handed a plain description of the queue -- no game
objects -- so the decision can be tested outside the game, which is where the
consequences of getting it wrong are cheap.
]]

PZAgent = PZAgent or {}

local Ownership = {}
PZAgent.Ownership = Ownership

local Protocol = nil

--- Resolved late so load order between shared modules does not matter.
local function protocol()
  Protocol = Protocol or PZAgent.Protocol
  return Protocol
end

Ownership.CLASS = {
  MOD = "mod",
  MANUAL = "manual",
  AMBIGUOUS = "ambiguous",
}

--- Prefix of the marker the mod stamps on actions it enqueues. Chosen so that
--- an unrelated mod's field cannot collide with it by accident.
Ownership.TAG_PREFIX = "pz_agent"

--- A queue longer than this is not a queue the mod can reason about. Scanning
--- stops there and the remainder is reported as foreign, which is the safe
--- direction: unscanned entries are never cleared.
Ownership.MAX_ENTRIES = 256

--- The marker to stamp on an action the mod enqueues.
function Ownership.tagFor(sessionId, commandId)
  if type(sessionId) ~= "string" or #sessionId == 0 then
    return nil, "session id is required to tag an action"
  end
  if type(commandId) ~= "string" or #commandId == 0 then
    return nil, "command id is required to tag an action"
  end
  return Ownership.TAG_PREFIX .. ":" .. sessionId .. ":" .. commandId
end

--- Decide who owns one queue entry.
---
--- `entry` is the plain description produced by the queue reader:
---   index         -- position in the queue
---   action_type   -- class name of the timed action, for diagnostics
---   agent_tag     -- the marker Ownership.tagFor produced, if any
---   agent_session -- the session that stamped the marker, if any
---   source        -- "player" when the takeover detector positively saw the
---                    player start this action
function Ownership.classify(entry, sessionId)
  if type(entry) ~= "table" then
    return Ownership.CLASS.AMBIGUOUS
  end
  if entry.source == "player" then
    return Ownership.CLASS.MANUAL
  end
  if type(sessionId) ~= "string" or #sessionId == 0 then
    -- Without a session there is nothing that could have stamped a tag, so
    -- nothing in the queue can be ours.
    return Ownership.CLASS.AMBIGUOUS
  end
  if type(entry.agent_tag) ~= "string" or type(entry.agent_session) ~= "string" then
    return Ownership.CLASS.AMBIGUOUS
  end
  if entry.agent_session ~= sessionId then
    -- A tag from an earlier session: the action outlived the session that
    -- created it, and this session did not queue it.
    return Ownership.CLASS.AMBIGUOUS
  end
  local expectedPrefix = Ownership.TAG_PREFIX .. ":" .. sessionId .. ":"
  if entry.agent_tag:sub(1, #expectedPrefix) ~= expectedPrefix then
    return Ownership.CLASS.AMBIGUOUS
  end
  return Ownership.CLASS.MOD
end

local function classifyAll(entries, sessionId)
  local classes = {}
  local counts = {
    [Ownership.CLASS.MOD] = 0,
    [Ownership.CLASS.MANUAL] = 0,
    [Ownership.CLASS.AMBIGUOUS] = 0,
  }
  local total = 0
  local truncated = false
  if type(entries) == "table" then
    total = #entries
    if total > Ownership.MAX_ENTRIES then
      truncated = true
    end
    local scanned = total
    if scanned > Ownership.MAX_ENTRIES then
      scanned = Ownership.MAX_ENTRIES
    end
    for index = 1, scanned do
      local class = Ownership.classify(entries[index], sessionId)
      classes[index] = class
      counts[class] = counts[class] + 1
    end
    for index = scanned + 1, total do
      classes[index] = Ownership.CLASS.AMBIGUOUS
      counts[Ownership.CLASS.AMBIGUOUS] = counts[Ownership.CLASS.AMBIGUOUS] + 1
    end
  end
  return classes, counts, total, truncated
end

--- The description of a queue that could not be read at all.
---
--- Deliberately not the same as an empty queue: "I looked and there was
--- nothing" permits the agent to enqueue work, while "I could not look" must
--- not. Reporting an unreadable queue as empty is the single most dangerous
--- mistake this module could make, so it has its own constructor.
function Ownership.unreadable()
  return {
    ownership = protocol().OWNERSHIP.AMBIGUOUS,
    busy = true,
    readable = false,
    total = 0,
    mod_owned = 0,
    foreign = 0,
    truncated = false,
    classes = {},
  }
end

--- Summarise the queue the way the observation schema reports it.
---
--- Ambiguous dominates because it is the least-known state; manual beats mod
--- because a queue the mod did not fully author is not the mod's. The result is
--- "mod" only when every entry is provably ours, which is what lets the
--- executor treat "ownership == mod" as permission to keep going.
---
--- `entries` of nil means the queue could not be read; see `unreadable`.
function Ownership.describe(entries, sessionId)
  if type(entries) ~= "table" then
    return Ownership.unreadable()
  end
  local classes, counts, total, truncated = classifyAll(entries, sessionId)
  local names = protocol().OWNERSHIP
  local ownership
  if total == 0 then
    ownership = names.NONE
  elseif counts[Ownership.CLASS.AMBIGUOUS] > 0 then
    ownership = names.AMBIGUOUS
  elseif counts[Ownership.CLASS.MANUAL] > 0 then
    ownership = names.MANUAL
  else
    ownership = names.MOD
  end
  return {
    ownership = ownership,
    busy = total > 0,
    readable = true,
    total = total,
    mod_owned = counts[Ownership.CLASS.MOD],
    foreign = counts[Ownership.CLASS.MANUAL] + counts[Ownership.CLASS.AMBIGUOUS],
    truncated = truncated,
    classes = classes,
  }
end

--- Which entries a panic stop may cancel.
---
--- Returns a plan whose `clear` list contains only entries proved to be this
--- session's. `whole_queue` says whether cancelling everything is equivalent to
--- cancelling the plan -- the caller needs that, because the only queue-clearing
--- API the game is known to expose clears the queue wholesale, and using it
--- while a foreign entry is present would destroy the player's work.
function Ownership.panicPlan(entries, sessionId)
  local classes, counts, total, truncated = classifyAll(entries, sessionId)
  local clear = {}
  local keep = {}
  local clearCount = 0
  local keepCount = 0
  for index = 1, total do
    local entry = type(entries) == "table" and entries[index] or nil
    local record = {
      index = index,
      class = classes[index],
      action_type = type(entry) == "table" and entry.action_type or nil,
    }
    if classes[index] == Ownership.CLASS.MOD then
      clearCount = clearCount + 1
      clear[clearCount] = record
    else
      keepCount = keepCount + 1
      keep[keepCount] = record
    end
  end
  local foreign = counts[Ownership.CLASS.MANUAL] + counts[Ownership.CLASS.AMBIGUOUS]
  return {
    clear = clear,
    keep = keep,
    mod_owned = clearCount,
    foreign = foreign,
    truncated = truncated,
    whole_queue = foreign == 0 and clearCount > 0,
  }
end

--- True when automation must not enqueue anything right now.
---
--- Anything the mod did not author -- including ambiguous -- counts, so the
--- agent waits rather than fighting the player for the action queue.
function Ownership.blocksAutomation(description)
  if type(description) ~= "table" then
    return true
  end
  return description.busy == true and description.ownership ~= protocol().OWNERSHIP.MOD
end

return Ownership
