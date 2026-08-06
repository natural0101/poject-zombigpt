-- PZAgent.Adapters.Literature: reading, and the two halves of proving it.
--
-- The postcondition is deliberately harder than "the page counter moved". The
-- groups below check both halves separately: a counter that moved with nobody
-- observed reading fails, and a build that cannot report reading at all fails as
-- a capability rather than being waved through.

local ROOT = arg[0]:match("^(.*)test_adapter_literature%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Inventory", "Literature" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Literature = PZ.Adapters.Literature
local Read = Literature.Read
local REASON = PZ.Protocol.REASON

--- Reading turns the character's reading flag on and advances the counter.
local function installRead(pagesPerRun)
  return Support.installAction("ISReadABook", function(_, args)
    local player, item = args[1], args[2]
    player.state.reading = true
    item.fields.pages_read = (item.fields.pages_read or 0) + (pagesPerRun or 25)
  end)
end

local function scene(options)
  options = options or {}
  local book = Support.item({
    id = 7,
    full_type = "Base.BookCarpentry1",
    name = "Carpentry Volume 1",
    pages = options.pages or 120,
    pages_read = options.pages_read or 0,
  })
  local rock = Support.item({ id = 9, full_type = "Base.Rock", name = "Rock" })
  local magazine = Support.item({ id = 3, full_type = "Base.Magazine", name = "Magazine", pages = 30 })
  local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { magazine } })
  local main = Support.container({ book, rock, satchel })
  local player = Support.player({ x = 100, y = 200, z = 0, inventory = main })
  if options.illiterate then
    player.state.traits.Illiterate = true
  end
  Support.installAction("ISWalkToTimedAction")
  Support.installAction("ISInventoryTransferAction", function(_, args)
    Support.moveItem(args[2], args[3], args[4])
  end)
  local read = installRead(options.pages_per_run)
  local queue = Support.installQueue()
  local ctx = Support.context({ player = player, safety = options.safety })
  return { player = player, ctx = ctx, read = read, queue = queue, book = book, main = main }
end

local BOOK = Support.itemRef("player-main", 7)
local ROCK = Support.itemRef("player-main", 9)
local MAGAZINE = Support.itemRef("carried:99", 3)

Harness.group("read refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Read:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "reading nothing in particular is not a command")

  code = refuse({ item_ref = BOOK, chapters = 3 })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ item_ref = BOOK, pages = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "zero pages is not a session of reading")

  code = refuse({ item_ref = BOOK, pages = 500 })
  equal(code, REASON.INVALID_ARGUMENT, "and more than the page budget is refused, not clamped")

  code = refuse({ item_ref = ROCK })
  equal(code, REASON.PRECONDITION_FAILED, "an item with no page count is not literature")

  local finished = scene({ pages = 120, pages_read = 120 })
  code = refuse({ item_ref = BOOK }, finished.ctx)
  equal(code, REASON.NO_SUITABLE_LITERATURE, "a copy read to the end has nothing left to give")
end

Harness.group("an Illiterate character is refused before anything is queued")
do
  local s = scene({ illiterate = true })
  local accepted, code, detail = Read:validate({ item_ref = BOOK }, s.ctx)
  isNil(accepted, "the command is refused")
  equal(code, REASON.POLICY_DENIED, "reading a book this character cannot read is a refusal, not a failure")
  contains(detail, "Illiterate", "and the detail names the trait")
  equal(#s.queue.added, 0, "nothing was queued")
end

Harness.group("a build with no trait check cannot promise the refusal, so it refuses")
do
  local s = scene()
  s.player.HasTrait = nil
  local accepted, code, detail = Read:validate({ item_ref = BOOK }, s.ctx)
  isNil(accepted, "without a trait check there is no way to honour the Illiterate rule")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "so the gap is reported as a capability")
  contains(detail, "HasTrait", "naming the accessor that was missing")
end

Harness.group("a missing read action costs a capability, not a crash")
do
  local s = scene()
  Support.removeAction("ISReadABook")
  local accepted, code, detail = Read:validate({ item_ref = BOOK }, s.ctx)
  isNil(accepted, "with no read action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISReadABook", "naming the symbol this build did not have")
  installRead()
end

Harness.group("read fetches the book, then queues one tagged reading action")
do
  local s = scene()
  local args = { item_ref = MAGAZINE, pages = 10 }
  ok(Read:validate(args, s.ctx), "a magazine in a satchel is readable")
  ok(Read:prepare(args, s.ctx), "prepare succeeds")
  equal(s.queue.added[1].Type, "ISInventoryTransferAction", "and fetches it with the same tagged transfer")
  ok(Read:begin(args, s.ctx), "the read starts behind the fetch")
  equal(#s.read.actions, 1, "one reading action was constructed")
  local entries = PZ.Safety.describeQueue(s.player)
  equal(PZ.Ownership.describe(entries, s.ctx.session_id).ownership, PZ.Protocol.OWNERSHIP.MOD, "tagged as ours")
end

Harness.group("reading stops for a horde before it starts and while it runs")
do
  local threatened = scene({ safety = Support.threatened() })
  local accepted, code = Read:validate({ item_ref = BOOK }, threatened.ctx)
  isNil(accepted, "a book is not something to open in a horde")
  equal(code, REASON.THREAT_INTERRUPTED, "the reflex guard's level is the reason")

  local s = scene()
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "with no threat it validates")
  ok(Read:begin(args, s.ctx), "and starts")
  equal(Read:progress(args, s.ctx), "running", "and is running")
  PZ.Safety.setDanger(s.ctx.safety, PZ.Protocol.DANGER.CRITICAL)
  local state, threatCode = Read:progress(args, s.ctx)
  isNil(state, "a horde arriving mid-chapter ends the reading")
  equal(threatCode, REASON.THREAT_INTERRUPTED, "with the interruption's own code")

  local taken = scene()
  ok(Read:validate(args, taken.ctx), "the same command validates elsewhere")
  ok(Read:begin(args, taken.ctx), "and starts")
  taken.ctx.safety.manual_takeover = true
  local takenState, takenCode = Read:progress(args, taken.ctx)
  isNil(takenState, "and the player picking up the controls ends it too")
  equal(takenCode, REASON.USER_TAKEOVER, "with the takeover code")
end

Harness.group("progress ends at the page budget rather than reading forever")
do
  local s = scene({ pages_per_run = 12 })
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "the command validates")
  ok(Read:begin(args, s.ctx), "and starts")
  equal(Read:progress(args, s.ctx), "running", "nothing has been read yet")
  s.read.actions[1]:perform()
  equal(Read:progress(args, s.ctx), "done", "twelve pages covers a budget of ten")
end

Harness.group("verify refuses a reading that was queued but never happened")
do
  local s = scene()
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "the command validates")
  ok(Read:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  Support.drainQueue(s.queue)

  local evidence, code, detail = Read:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a queue that emptied with nobody reading proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed")
  contains(detail, "never observed reading", "and the detail names the half that was missing")

  s.read.actions[1]:perform()
  local proof = Read:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "a character seen reading with an advanced counter is evidence")
  equal(proof.kind, "pages_read", "named for what was observed")
  equal(proof.reading_started, true, "carrying the half that says reading began")
  equal(proof.pages_before, 0, "and the counter before")
  equal(proof.pages_after, 25, "and after")
end

Harness.group("a counter that moved with nobody reading is not this command's doing")
do
  local s = scene()
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "the command validates")
  ok(Read:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  -- The counter advances but the character was never observed reading.
  s.book.fields.pages_read = 40
  local evidence, code, detail = Read:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "an advanced counter alone is not the postcondition")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails")
  contains(detail, "never observed reading", "naming the half that did not hold")
end

Harness.group("a character seen reading whose counter never moved has read nothing")
do
  local s = scene()
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "the command validates")
  ok(Read:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  s.player.state.reading = true
  local evidence, code, detail = Read:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "holding a book open is not reading it")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails")
  contains(detail, "counter stayed at", "and the detail carries the counter that did not move")
end

Harness.group("a build that cannot report reading cannot prove it either")
do
  local s = scene()
  local args = { item_ref = BOOK, pages = 10 }
  ok(Read:validate(args, s.ctx), "the command validates")
  ok(Read:begin(args, s.ctx), "and starts")
  s.player.isReading = nil
  local before = Toolkit.observe(s.player)
  s.book.fields.pages_read = 40
  local evidence, code, detail = Read:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "with no reading accessor the counter cannot be attributed to this command")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not a failed postcondition")
  contains(detail, "isReading", "naming the accessor that would have settled it")
end

Harness.group("the adapter is registered where the dispatcher looks for it")
do
  equal(PZ.Adapters.BY_NAME["literature.read"], Read, "read is registered under its action name")
  ok(PZ.Protocol.isKnownAction(Read.name), "the name is in the protocol whitelist")
  equal(Read.capability, "read_literature", "and it declares the capability the sidecar gates on")
end

Harness.group("the declaration is what the dispatcher builds the args from")
do
  -- The page budget is the only thing that ends this command, so a build where
  -- the dispatcher dropped it would hand the adapter a book and a character who
  -- reads until something else stops them.
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local read = checkArgs(Read, { item_ref = BOOK, pages = 10 }, Support.SESSION)
  equal(read.item_ref, BOOK, "the item reference survives the rebuild")
  equal(read.pages, 10, "and so does the budget that ends the command")

  local _, longCode = checkArgs(Read, { item_ref = BOOK, pages = 500 }, Support.SESSION)
  equal(longCode, REASON.INVALID_ARGUMENT, "a budget past the ceiling never reaches the adapter")

  local _, zeroCode = checkArgs(Read, { item_ref = BOOK, pages = 0 }, Support.SESSION)
  equal(zeroCode, REASON.INVALID_ARGUMENT, "and neither does a session of no reading")

  local _, fractionCode = checkArgs(Read, { item_ref = BOOK, pages = 2.5 }, Support.SESSION)
  equal(fractionCode, REASON.INVALID_ARGUMENT, "pages are whole things")

  local _, unknownCode = checkArgs(Read, { item_ref = BOOK, chapters = 3 }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, missingCode = checkArgs(Read, { pages = 10 }, Support.SESSION)
  equal(missingCode, REASON.INVALID_ARGUMENT, "and reading nothing in particular is not a command")
end

Harness.finish("adapter_literature")
