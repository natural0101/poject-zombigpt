--[[
PZAgent.Adapters.Literature -- literature.read.

Invariant: reading is ISReadABook and the postcondition has two halves, both
observed. The page counter must have advanced *and* the character must have been
seen reading. Either alone is not enough: a page counter can move for reasons
this command did not cause, and a character seen holding a book has not
necessarily read a word of it.

Both halves are observations, so a build that exposes neither `isReading` nor a
trait check costs a CAPABILITY_UNAVAILABLE naming the accessor rather than a
success claimed on the strength of a missing reading. That is the deliberate
trade: on such a build reading is simply not a thing this agent will claim to
have done.

Illiteracy is checked before anything is queued, because the game will happily
accept the action and the character will read nothing -- the page counter then
stays put and the command fails several seconds later with a far less useful
reason than "this character cannot read".

The page budget is a bound on how long one command occupies the character, not a
limit the engine enforces: ISReadABook keeps going until it is cancelled, so
progress reports `done` at the budget and the runtime's own cancellation is what
ends it. That is why the budget is small enough to be re-issued.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walked
-- adapters/ in an order that ran this file before Toolkit.lua. The statement
-- form is deliberate -- the paren form is banned as dynamic loading -- and the
-- test harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Literature = {}
PZAgent.Adapters.Literature = Literature

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Pages one command may cover. Deliberately a couple of minutes of game time:
--- a shorter command is re-issued cheaply, and a long one holds the character
--- still through a horde it could have been told about between steps.
Literature.DEFAULT_PAGES = 20
Literature.MAX_PAGES = 200

Literature.TIMEOUT_MS = 120000
Literature.POLL_MS = 500

local REQUIRES = { "ISReadABook", "ISReadABook.new", "ISTimedActionQueue.add" }

local READ_ARGS = { "item_ref", "pages" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  NUMBER = "number",
  REF = "ref",
}

--- Is this character able to read at all?
---
--- Returns true, false, or nil plus the accessor that was missing. Never a
--- default: "the trait check is unavailable" and "the character is literate"
--- are opposite answers to the same question.
function Literature.isIlliterate(player)
  local Toolkit = toolkit()
  local ok, hasTrait = Toolkit.call(player, "HasTrait", "Illiterate")
  if ok and type(hasTrait) == "boolean" then
    return hasTrait
  end
  local okTraits, traits = Toolkit.call(player, "getTraits")
  if okTraits and traits ~= nil then
    local okContains, contains = Toolkit.call(traits, "contains", "Illiterate")
    if okContains and type(contains) == "boolean" then
      return contains
    end
    local size = Toolkit.listSize(traits)
    if size ~= nil then
      for index = 0, math.min(size, Toolkit.MAX_WORN) - 1 do
        if Toolkit.listGet(traits, index) == "Illiterate" then
          return true
        end
      end
      return false
    end
  end
  return nil, "IsoPlayer.HasTrait"
end

local function readSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, READ_ARGS, { "item_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local pages, pagesCode, pagesDetail = Toolkit.readCount(args, "pages", {
    default = Literature.DEFAULT_PAGES,
    minimum = 1,
    maximum = Literature.MAX_PAGES,
  })
  if pages == nil then
    return nil, pagesCode, pagesDetail
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local identity = tonumber(parsed.runtime_id)
  if identity == nil then
    return nil, reasons.INVALID_REF, "the item reference carries no numeric identity"
  end
  return { item_ref = itemRef, identity = identity, pages = pages }
end

Literature.readSpec = readSpec

local Read = nil

local function readValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Read.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = readSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local illiterate, missing = Literature.isIlliterate(ctx.player)
  if illiterate == nil then
    return Toolkit.unavailable(missing)
  end
  if illiterate then
    return nil, reasons.POLICY_DENIED, "this character is Illiterate and would read nothing"
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local total = Toolkit.readNumberOf(item, { "getNumberOfPages" })
  if total == nil then
    return nil, reasons.PRECONDITION_FAILED, "the item reports no page count, so it is not literature"
  end
  local done = Toolkit.readNumberOf(item, { "getAlreadyReadPages" })
  if done ~= nil and done >= total then
    return nil, reasons.NO_SUITABLE_LITERATURE, "this copy has already been read to the end"
  end
  -- Reading holds the character still and deaf; starting one next to a horde is
  -- the situation the reflex guard exists for.
  local threatCode, threatDetail = Toolkit.interruption(ctx)
  if threatCode ~= nil then
    return nil, threatCode, threatDetail
  end
  Toolkit.state(ctx).read = spec
  return true
end

--- ISReadABook wants the book in the character's own inventory.
local function readPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = readSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local outcome, moveCode, moveDetail = PZAgent.Adapters.Inventory.bringToMain(ctx, spec.item_ref)
  if outcome == nil then
    return nil, moveCode, moveDetail
  end
  return true
end

local function readStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = readSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local state = Toolkit.state(ctx)
  -- Where the counter stood when the command started, so the budget is measured
  -- against this command's reading rather than against the book's whole history.
  state.pages_at_start = Toolkit.readNumberOf(item, { "getAlreadyReadPages" })
  local action, actionCode, actionDetail = Toolkit.construct("ISReadABook", ctx.player, item)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

--- Record what this poll saw of the character's reading state.
---
--- Two separate facts: whether the accessor exists at all, and whether it ever
--- answered true. Collapsing them would turn a build with no accessor into a
--- character who was seen not reading.
local function noteReading(ctx, snapshot)
  local state = toolkit().state(ctx)
  if type(snapshot.reading) == "boolean" then
    state.reading_accessor = true
    if snapshot.reading then
      state.reading_observed = true
    end
  end
  return state
end

local function readProgress(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = readSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local snapshot = Toolkit.snapshot(ctx)
  local state = noteReading(ctx, snapshot)
  local record = snapshot.items ~= nil and snapshot.items[spec.identity] or nil
  if record ~= nil and record.pages_read ~= nil and state.pages_at_start ~= nil then
    if (record.pages_read - state.pages_at_start) >= spec.pages then
      return "done"
    end
  end
  return Toolkit.queueProgress(ctx)
end

local function readVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = readSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local state = noteReading(ctx, after)
  local accessor = state.reading_accessor == true
    or type(before) == "table" and type(before.reading) == "boolean"
    or type(after) == "table" and type(after.reading) == "boolean"
  if not accessor then
    -- Without this reading there is no way to tell a page counter that moved
    -- because of this command from one that moved for any other reason.
    return Toolkit.unavailable("IsoPlayer.isReading")
  end
  local started = state.reading_observed == true
    or (type(before) == "table" and before.reading == true)
    or (type(after) == "table" and after.reading == true)
  if not started then
    return nil, reasons.POSTCONDITION_FAILED, "the character was never observed reading"
  end
  local was = type(before) == "table" and before.items ~= nil and before.items[spec.identity] or nil
  local now = type(after) == "table" and after.items ~= nil and after.items[spec.identity] or nil
  if was == nil or now == nil or was.pages_read == nil or now.pages_read == nil then
    return nil, reasons.POSTCONDITION_FAILED, "this build reports no page counter for the item"
  end
  if now.pages_read <= was.pages_read then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("the character started reading but the counter stayed at %d", now.pages_read)
  end
  return {
    kind = "pages_read",
    item_ref = spec.item_ref,
    reading_started = true,
    pages_before = was.pages_read,
    pages_after = now.pages_read,
    pages_requested = spec.pages,
    pages_total = now.pages,
  }
end

Read = toolkit().declare({
  name = "literature.read",
  capability = toolkit().CAPABILITY.READ_LITERATURE,
  requires = REQUIRES,
  timeout_ms = Literature.TIMEOUT_MS,
  poll_interval_ms = Literature.POLL_MS,
  args = {
    item_ref = { type = ARG.REF, required = true, kinds = { item = true } },
    -- Whole pages, and a ceiling: this is the budget that ends the command, and
    -- an unbounded one is a character reading until something else stops them.
    pages = { type = ARG.NUMBER, integer = true, min = 1, max = Literature.MAX_PAGES },
  },
  validate = readValidate,
  prepare = readPrepare,
  begin = readStart,
  progress = readProgress,
  verify = readVerify,
})

Literature.Read = Read
toolkit().register(Read)

return Literature
