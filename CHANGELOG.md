# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

### Fixed

- **The mod's two safety gates had no test, and one of them is the whole
  meaning of «Остановился.»** (`dev`). The Lua half — the code that runs inside
  Project Zomboid, with no Python layer between it and the character — had never
  been swept. A four-area sweep of 299 refusal sites returned 18 findings against
  27 refusals it found properly guarded; these are the first three closed, each
  re-planted here and each guard shown to fail under its plant.

  - **`ActionRuntime.verify` refusing POSTCONDITION_MET when every before/after
    pair reads identical.** FALSE SUCCESS at its single choke point. The
    empty-evidence half of this gate was tested; this half was not, and it is
    the one that matters more, because an adapter carrying a full, well-formed
    evidence bag looks exactly like one that worked. The sidecar cannot
    re-derive it: `ActionResult.succeeded()` refuses only an *empty* bag. A
    second test pins the `unchanged_is_success` exemption, without which the
    first adapter that legitimately ends where it started would be told to
    delete the gate.
  - **`StopAdapter` re-reading the safety state and refusing to report success
    unless it observes `armed == false`.** This is the one place that checks the
    stop actually landed; nothing downstream re-checks. Under the plant the ack
    is `POSTCONDITION_MET` for a stop that left the agent armed — while every
    operator document in this repository tells the user that «Остановился.»
    means the agent stopped. The stub needed a full outcome before the plant
    showed that: with a truncated one it crashed on the lines below instead,
    which is a catch for the wrong reason.

- **The mod's refusal of an unreadable action queue, and the reason no test
  could reach it** (`dev`). `queueObject` refuses a queue object whose entry
  list is missing or is not a table. Nothing exercised it, and the cause was in
  the harness rather than in anybody's attention: `Mock.installActionQueue`
  always builds `{ queue = entries or {} }`, so every test that installed a
  queue handed the mod an object whose entry list was already a table. The
  branch was structurally unreachable, and deleting the refusal outright left
  all 33 Lua files and the contract suite byte-identically green.

  Three groups read as coverage of it and are not. The closest —
  *"a queue that could not be read is never reported as clear"*, whose name is
  precisely the property — calls `Mock.removeActionQueue()` first, which trips
  the *API-absence* refusal above and returns before the shape check is ever
  evaluated. Its assertions hold whether or not the guard exists.

  The two shapes fail in opposite directions, so the new group drives both.
  An entry list under another name makes `#queue.queue` raise — and nothing
  catches it: `Runtime.stop` calls `describeQueue` as its *first* statement,
  before the disarm, and the panic-key path is not `pcall`-wrapped, so the raise
  would take the engine event handler down and leave the agent armed. A field
  that is present, is not a table and still answers `#` raises nothing at all:
  the mod reads zero entries, calls the queue readable, and `applyStop` reports
  `VERIFIED` — "nothing the mod owns is queued" — for a queue it never read.
  `Mock.installMalformedActionQueue` is the door into the branch.

- **Four more mod refusals: the engine raise, the control plane, capability
  honesty, and an infinity no bound would catch** (`dev`).

  `queueObject` wraps the engine call in `pcall`, which is the third way the
  queue read can fail and the one the shape check cannot cover: the symbol
  exists and calling it throws. Where it escapes to is the point — `Runtime.stop`
  calls `describeQueue` ahead of the disarm and the panic key is not
  `pcall`-wrapped, so a raise takes the engine event handler down and the agent
  stays armed.

  `ActionRuntime.install` refuses to let a published adapter displace a built-in
  control adapter. Four actions are exempt from the ordinary "published wins"
  rule because they drive the runtime's own state; the comment says why — "a
  stop that did not cancel the in-flight command would be a stop in name only" —
  and nothing tested the exemption.

  `CapabilityRuntime.Handle:publish` refuses to stamp the revision when the
  write failed. `needsPublish()` is `published_revision ~= revision`, so
  stamping it on a failure makes the report never go out again while the mod
  believes it published.

  `checkNumber` refuses the infinities before any bound is compared — and the
  first version of that test proved nothing. Against a spec with `integer` and
  `min`/`max`, the other checks catch NaN and both infinities on their own, so
  the plant sailed through. The shipped declaration where the guard is alone is
  `movement.move_to`'s `x` and `y`: required integers with no bounds, because a
  square's coordinates are not bounded by anything the mod knows. `math.floor
  (inf) == inf`, so an infinity passes the integer check and arrives as a
  coordinate. NaN is named in the same guard and is *not* this test's evidence —
  the integer check catches it too, and saying so keeps the guard's unique
  contribution from being overclaimed.

### Measured and open

- Ten findings from the Lua sweep are **not yet guarded**: the mod's byte
  caps on journal lines, whole documents and reads (`Ipc.lua`), session
  eviction (`Session.lua`), the queue-shape and engine-raise refusals and the
  undated-threat rule (`Safety.lua`), the JSON decoder's depth bound and the
  encoder's key cap (`Json.lua`), the zombie-scan cap (`Observe.lua`), the
  reference byte cap (`Refs.lua`), the token-list cap (`ObserveModel.lua`), the
  built-in control adapter's precedence and the capability-publish honesty rule
  (`ActionRuntime.lua`, `CapabilityRuntime.lua`), and `checkNumber`'s refusal of
  NaN and the infinities (`CommandDispatcher.lua`) — the same defect family as
  the loot weight guard closed two rounds ago. Each has a measurement behind it.

  One of them deserves naming now because it is the same shape as a defect this
  project has already paid for: `Safety.applyStop` re-reads the queue after
  clearing it, and an *unreadable* re-read is deliberately indistinguishable
  from an observed-empty one — so without the guard a stop over a queue it could
  not read reports the full count as cleared.

- **The coverage guard was exactly as wide as its own set, and its set was
  "Python packages" when the question was "shipped code"** (`dev`). The check
  added last round derives the subpackage list from `packages/` and requires
  `docs/PROGRESS.md`'s sweep claim to name every member. It did its job: it
  caught five names the corrected paragraph was still missing.

  It could not catch this one. `pz-mod/42/media/lua/` — the mod, the half that
  actually runs inside Project Zomboid — is not under `packages/`, so the
  derivation never saw it, no coverage claim mentioned it either way, and it had
  never been swept. The guard passed a paragraph that named every Python
  subpackage and said nothing about twenty-odd Lua modules.

  The derivation now includes the mod, as one unit rather than module by module:
  the claim being checked is which *areas* the sweep has been run against, and
  listing twenty file names in a paragraph is noise nobody reads. The claim
  itself records the Lua half as **unmeasured**, which is different from both
  "sound" and "broken", until the sweep now running against it reports.

- **Five unguarded refusals in the packages the sweep had never been given —
  the sidecar lock, the RPC descriptor, the MCP argument validator and the
  planner's view of the world** (`dev`). Each was re-planted here and each guard
  shown to fail under its plant.

  - **`SidecarLock._break_stale`'s re-read.** The classic time-of-check /
    time-of-use window: `acquire` reads a holder, judges it stale, and in the
    gap before the unlink a merely-stalled holder can wake and `refresh()`. The
    comparison of `refreshed_at_ms` is what notices. Without it the woken
    holder's lock is deleted under it and two sidecars share one exchange
    directory — the failure this file exists to prevent.
  - **The neighbouring owner comparison**, for the three-way race where a third
    process claims the lock while a second is breaking it. Measured rather than
    assumed: removing it alone changes nothing, because in any realistic race
    the newcomer's record is also *fresher* and the check above catches it
    first. Removing both fails the new test, so the guarantee is pinned rather
    than whichever comparison delivers it.
  - **`load_descriptor`'s pre-parse depth bound.** `json.loads` recurses once
    per nesting level, so a file of three thousand open brackets — well inside
    the 8 KiB byte cap — spends the interpreter's stack and raises
    `RecursionError`, which is not the `DescriptorError` the loader promises and
    cannot safely be caught after the fact.
  - **The MCP validator's array ceiling clamp.** `MAX_ARRAY_ITEMS` is one of
    three process-protecting bounds on caller-supplied input; the clamp is what
    makes a schema's `maxItems` a narrowing rather than a raise. The existing
    test exercised only the default branch, where no `maxItems` is declared at
    all, so it passed with the clamp gone.
  - **`_unread`'s cap in `compact_for_planner`.** The keys come from
    `player.stats`, an open map the mod fills and grows. Names were
    token-checked; how many there were was not, in the one payload a model ever
    sees.

- **A flood fill that does not terminate without its `visited` guard** (`dev`).
  `enclosure_after` is the check that decides whether placing a wall would seal
  the character in. Its breadth-first fill remembers where it has been; delete
  that and `visited` — a set — stops growing while the queue does not, so
  `MAX_FLOOD_SQUARES` is compared against a number that has stopped moving and
  the search never returns. The module docstring calls the fill bounded and
  nothing tested it.

  The first version of this test used an *open* window and passed under the
  plant: the fill leaves through the first edge square it reaches, long before a
  cycle could bite. It now seals the window's outer ring, which is the case the
  guard exists for, and asserts the work done rather than waiting — a
  non-terminating loop caught by a three-hundred-second timeout is a worse
  failure report than one caught at a stated ceiling.

- **The three mission refusals: a poisoned weight, an unbounded arrival radius,
  and half of the stuck detector** (`dev`).

  `_safe_weight` reads an item weight that is not a finite non-negative number
  as weightless. Weights come off the wire and `_as_float` checks only the JSON
  type, so NaN, infinity and negatives all reach the selection — where one of
  them destroys the total order the weight-ascending sort depends on *and*
  poisons the running capacity budget for every later item. The new test needed
  two assertions before it caught anything: the three kinds of rubbish fail in
  opposite directions, NaN and a negative making the budget accept an item that
  does not fit while infinity makes it reject one that does.

  `NavigationTarget.__post_init__` bounds the arrival radius. The radius is the
  only thing that turns an observed position into `Arrived`, and
  `_check_arrived` runs before any action is emitted — so an oversized one
  declares arrival from an arbitrary distance without the character moving and
  without the mod being asked.

  `Journey._check_stuck` counts two things, and only one was tested. The mod
  acks `movement.move_to` as SUCCEEDED when the queue entry completed, not when
  the character arrived, so an oscillating route or a shoved character produces
  a run of *successful* legs that get no closer — invisible to the failure
  counter, which never leaves zero. Honest about the size of it:
  `max_legs` still stops the journey, so termination survives; what is lost is
  the honest STUCK diagnosis in place of "ran out of legs".

- **Four more: a type invariant, a typed read failure, a bound on a foreign
  port's answer, and a redaction on the way to a model** (`dev`).

  `DrinkChoice`'s constructor refuses a choice that says "whole container" and
  "half of it" at once. `_choose_fraction` returns a whole container precisely
  when the build has no `drink_percentage` probe — such a build cannot pour half
  a bottle — so the pairing is the capability-honesty rule as a type invariant.
  The range check beside it is a different rule and 0.5 passes it happily.

  `load_report`'s read handler turns an unreadable capability ledger into the
  one typed failure its callers catch. `pz-agent doctor` and the runtime both
  wrap the load in `except (ReportIOError, ScanError)`, so a bare `OSError`
  becomes a traceback rather than a reported problem. The stat handler four
  lines above is what makes this one look redundant, and is not: a file can be
  stattable and still fail to read.

  The MCP router's `islice` is the boundary's own ceiling on how much a foreign
  memory port may put in one answer. In production the core honours the limit
  that was sent — but "the peer behaved" is not a bound, and every record past
  it would otherwise be scrubbed, encoded and handed to a model.

  And a capability's `reason` is scrubbed before `pz://capabilities` serves it.
  The reason is written by the probe layer, which is the layer that knows about
  install paths; `Capability.__post_init__` bounds its length without redacting
  it, and the neighbouring `withheld_tools` scrub covers a different field.

- **The last two, both in `rpc/`, and with them every finding the sweep
  produced** (`dev`).

  `RpcClient._dial` arms the socket with the call's deadline before connecting.
  This is the one wait that happens before anything else bounds it: `call`
  arms its watchdog and poll guard on the connection the dial *returns*, so
  neither exists while the connect is in progress. A sidecar that is alive — so
  the descriptor's liveness check passes — but whose socket is not accepting
  leaves the connect to the kernel's own timeout. The existing test that an
  absent address "fails fast" passes with the line deleted, because it fails
  fast for an unrelated reason: nothing is listening. The new test observes the
  order of calls on the socket, which is what the line actually is.

  `_alive`'s `PermissionError` arm reads a pid owned by another account as
  alive. `os.kill(pid, 0)` answers three ways and only two were tested; the
  third is a sidecar started by a service user or another session, and reading
  it as anything but alive means overwriting a live address. It also fails in
  the wrong shape: `PermissionError` is an `OSError`, and `load_descriptor`'s
  own `except OSError` sits around the file read far above, so without the arm
  the exception escapes the loader as a traceback rather than a refusal.

  That closes all fifteen findings from the five-area sweep. What remains
  unmeasured is stated where it belongs, in `docs/PROGRESS.md`: within each area
  only a handful of refusal sites were planted, so "swept" means "sampled under
  a stated budget".

- **A coverage claim that named nine areas and read as though it named the
  tree** (`dev`). `docs/PROGRESS.md` recorded the refusal-plant sweep as having
  covered "every area of shipped code". It had covered every area it was
  *given* — nine — and the tree holds twenty-one: the whole of `pz_agent_mcp`
  and eleven more subpackages inside the core had never been assigned to an
  agent, so they had never been measured while the sentence implied they had.

  The claim is corrected to name each one, in the swept list or the not-yet
  list, and `tests/contract/test_the_sweep_coverage_claim_names_the_tree.py`
  derives the subpackage set from `packages/` and requires every member to
  appear. It deliberately cannot tell swept from unswept — that is a fact about
  work done, not about the tree, and a test that tried to decide it would be
  asserting the document against itself — but it catches the case that
  happened, a package the paragraph never mentions. It earned itself on the
  first run by finding five more names the corrected paragraph was still
  missing.

- **The three voice refusals recorded as open last round are settled, and all
  three were real** (`dev`). Each was re-planted here and each new test shown to
  fail under its own plant.

  - **The panic latch's post-write size check.** Every ordinary way the write
    fails already raises — a directory in its place, a read-only exchange, a
    full disk — and both the raise and the companion's `STOP_FAILED` sentence
    were already tested. What this check adds is the case those cannot produce:
    the write returns and the file is empty anyway. The mod reads any non-empty
    content as a stop and empty as nothing, so the user would be told
    «Остановился.» while the agent kept acting. The new test makes the write
    report success and land nothing, which is precisely the case the line is for.
  - **`_log_safely`'s `OSError` handler.** `_companion_log` catches only at
    *construction*, so an unwritable logs directory at start is handled; this is
    for the tenth record, when a directory fills mid-session. The diagnostic log
    writes through a real rotating file and lets `OSError` out, and both call
    sites that matter run outside `_serve`'s only `try` — so without the handler
    the user gets a traceback instead of the ending sentence, from a failure
    that has nothing to do with the agent.
  - **Redaction of the line `voice run` prints.** On the failure branch the
    ending sentence embeds the backend exception verbatim, and an `OSError` or
    SDK error routinely carries an absolute path — while the redactor is built
    from exactly those tokens. The record and the log keep their own redaction
    and the support bundle redacts every member, so the archive was covered;
    stdout was what nothing protected. `test_voice_privacy.py` missed it because
    it scans for a *transcript* canary, not for paths.

- **Nine more unguarded refusals — the IPC journal and queue, the goal channel's
  restore, and the planner's HTTP transport** (`dev`), from a five-area plant
  sweep. Each was re-planted here and each new test shown to fail under its own
  plant. `memory/` was swept the same way and is reported **sound** — five
  plants, all five caught.

  *IPC.* `probe_header` reported an oversized header as "nothing to report"
  rather than as a problem, so a permanently unreadable journal would poll
  empty and healthy for ever — FALSE SUCCESS on the path carrying the game's
  answers. `_consume`'s `MAX_READ_BYTES` was the only bound on how much of a
  journal one poll pulls into memory, and it runs on rotated segments where the
  record budget is deliberately `None`. And `_highest_committed_seq`'s
  `pending_bytes > 0` is the signal for exactly how a process dies — mid-write,
  bytes after the header with no committed newline; without it recovery answers
  "nothing here", the restarted queue seeds the command stream at zero, and the
  sidecar becomes a second producer of numbers the mod accepts because it dedups
  by `command_id` rather than `seq`. That is the live Build 42.20.2 finding,
  half of it unguarded.

  *Goals.* `GoalQueue.restore` refuses a snapshot naming one goal id twice and
  one reusing an idempotency digest; nothing else on the load path compares
  records to each other. Without the first, a restore silently drops a goal —
  perhaps the pending one, perhaps an active one owed back as
  `SESSION_TERMINATED` — and `lost` is the caller's only account of the restart.
  Without the second, a retried submission resolves to the wrong goal. Also
  `snapshot_from_document`'s cap on the stored terminal history, which is the
  reader half of the writer's truncation.

  *Planner transport.* The body ceiling bounded the *read* — and the existing
  test passed either way, because the `len(body) > limit` check two lines below
  still fires once the whole body is on the heap. The read timeout is separate
  from the connect timeout only because the live socket is re-armed after
  connecting; with that line gone, the shipped 60s read budget silently becomes
  5s, aborting exactly the slow local model it exists for. And a peer sending
  `Content-Length: banana` produced a bare `ValueError` that no handler in the
  transport or either provider catches — a traceback where a named transport
  failure belongs.

- **The section that answers "what still needs the game" named the smaller of
  the two scenario catalogues and not the one the release stands on** (`dev`).
  `docs/PROGRESS.md`'s *Requires a live game session* listed the 16 definitions
  under `tests/game-smoke/`. Nothing in that was false — they exist and
  `pz-agent smoke` drives them — but `scripts/check_release.py --release` does
  not read them. It requires a `PASS` and hashed artefacts for every id in
  `SCENARIO_IDS`, the 22-scenario catalogue behind `pz-agent live-test`, and the
  section named it nowhere.

  The cost is concrete because the two catalogues number their scenarios
  independently and the numbers collide: `S14` is `backup / restore` in one and
  `SLEEP_REST` in the other. A reader of that section got the wrong list *and* a
  release bar three times smaller than the real one — and `docs/RELEASE.md`
  makes that section the source for the list of steps in
  `FINAL_IMPLEMENTATION_REPORT.md` that physically require the game.

  `tests/contract/test_the_live_bar_is_named_where_it_is_owed.py` now requires
  the section to state the live catalogue's size — taken from `SCENARIO_IDS`,
  not typed into the test — and to name the gate that reads it. A control
  requires the smoke catalogue to survive the fix, so one omission cannot be
  traded for the other; planting confirms it is independent, since dropping the
  new paragraph fails the two checks about the live catalogue and leaves the
  control green. A fourth test fails if the two catalogues ever come to hold the
  same number of scenarios, because the count check could no longer tell which
  one the section had named.

- **Seven more unguarded refusals, across the adapters, the CLI loop and the
  backup subsystem** (`dev`), from the same multi-agent plant sweep. Each was
  re-planted here before a test was written for it, and each new test was shown
  to fail under its plant.

  - **`find_by_identity` compared runtime ids and generations, and only the
    runtime id was ever tested.** This is the recogniser every postcondition in
    the adapter layer runs on — transfers, batch transfers, wearing, eating,
    bandaging. Project Zomboid reuses runtime ids; the generation is in the
    identity because a bump means a save/load boundary, after which equal
    runtime ids say nothing. With that half dropped, a reference minted before a
    save/load matches whatever object now holds the id, and the adapter reports
    success about an object it never touched. `tests/unit/test_adapter_identity.py`
    now pins both directions: the same object is still found after it changes
    container, and a pre-bump reference matches nothing after it.
  - **`consume.drink_source` refused a vessel already holding tainted or
    poisonous fluid, untested.** `policy.drink` refuses unsafe water when it
    *selects* a bottle and that is tested; this path selects nothing — the
    vessel is named in the command, topped up at a sink and drunk. Topping up
    does not empty it, so the mod's own checks, which read the source, can pass
    over residue the source never contributed.
  - **`consume.drink_source` refused a destroyed vessel, untested.** Its
    postcondition is thirst alone, so the only possible end was a timeout with
    the character standing at the sink.
  - **`_arm_confirmed_by_heartbeat` required the heartbeat to be no older than
    the request, untested.** Session and mode were pinned; the freshness bound
    was not. It is what stops an `armed=true` heartbeat left on disk from before
    a crash from confirming an arm the current game process never granted.
  - **`_apply_control` refused an arm that landed on the same tick as a
    disarming safety event — and the test for it passed with the arbitration
    deleted.** Since the arm became two-phase, `armed is False` one tick after a
    request is true whether the guard refused it or the loop merely submitted
    `session.arm` and is waiting. The test now asserts what distinguishes them:
    an empty command journal and a decision saying why.
  - **A panic stop during a pending arm.** Measured rather than assumed, and the
    measurement corrected the finding: two levers cover this — the panic level's
    disarm and a branch in `_watch_pending_arm` — and removing *either* alone
    changes nothing observable, which is why planting them one at a time made
    each look unguarded. Removing both does break it, and now fails a test. The
    guarantee is pinned, not whichever line delivers it on a given tick.
  - **`create`'s post-landing check** that the manifest is where the backup was
    moved to. Without it `create` returns a `BackupRecord` naming a directory
    that is not there — and the caller that matters is `live-test prepare`,
    which takes "a backup exists" as its licence to arm twenty destructive
    scenarios.

  One suspicion from the sweep was **refuted** by measuring it: `create`'s
  staging-cleanup path was reported as probably unguarded because the tests
  assert `list(root.glob("*")) == []` and a staging directory is named
  `.staging-…`. `pathlib` globbing is not shell globbing and does match leading
  dots; the plant was duly caught by two tests.

- **Three unguarded refusals in the save-backup subsystem, found by a
  multi-agent plant sweep** (`dev`). The sweep gave four agents a disjoint area
  of shipped code each, in its own git worktree, with one rule: a refusal counts
  as unguarded only when its plant leaves the *full* suite green. `platform/`
  returned three, each re-planted in the main tree here before a line of test
  was written:

  - **`_verify_record`'s traversal bound.** The verify-side twin of the
    create-side cap, and the create-side one has had a test since the beginning.
    Deleting it left the suite green at 9484 passed. Verification and restore
    both walk the backup's data directory, so without the bound the operation a
    user runs *because* they are worried about a save is the one that
    materialises a path per entry and dies as exhausted memory rather than as a
    named refusal. Now pinned by counting what the walk visits — a cap checked
    after the listing protects nothing — and separately for `restore`, whose
    pre-flight verification is a different line from the bound itself.
  - **`_plan`'s "not a regular file".** Its neighbour one line above, the
    symlink refusal, was tested; this one was not. It carries two failures: a
    named pipe in a save directory would be *opened* by the copy — a backup that
    blocks forever instead of refusing in a sentence (planted, and the test
    duly hung until the timeout) — and, far likelier, a file the live game
    rotated away between the walk and the plan would escape as a raw
    `FileNotFoundError`, which this module's contract says must never happen
    because the CLI renders its refusals and lets anything else through as a
    traceback.
  - **`restore`'s postcondition.** The only place the restore path observes its
    own result. With it deleted the suite stayed green at 9464 passed, and a
    restore whose swap landed nothing still returns a populated `RestoreResult`
    naming a file count and a byte total — the CLI prints success, and the user,
    told the save is back, is free to prune the backup that was the last copy of
    it. AGENTS.md: `succeeded` means a postcondition was *observed*.

  No product behaviour changed; all three refusals were correct and unwatched.
  `safety/` was swept the same way — five plants, all five caught — and is
  reported sound.

- **A test that failed because of where the repository was checked out**
  (`dev`). `test_the_records_own_message_does_not_survive_into_the_traceback`
  renders a traceback and asserts the parametrised number does not appear in it.
  A rendered traceback quotes the path of every frame, so the `[-1]` case failed
  in any checkout whose path contains `-1` — which a git worktree named `…-1`
  does. It cost a real measurement: an agent sweeping an unrelated area had to
  stash its work to prove the failure was not its own, and every finding it
  reported carried a caveat about it.

  The paths are now removed from the rendering before the search, rather than
  the search weakened: messages, echoed source lines and the "During handling of
  the above exception" preamble all stay, which is the whole reason the test
  renders a traceback instead of reading `__cause__`. A control hands the same
  helper the exact regression it guards against — a raise with the `from None`
  dropped — and asserts the number is still found, so a substitution that ate
  too much cannot make the test pass for the worst possible reason.

- **Three of `live-test prepare`'s six refusals had no test, including the one
  that tells a backup that exists from a backup that restores** (`dev`).
  `prepare` is the subcommand that proves a world is safe to experiment on
  before twenty scenarios wound the character, interrupt actions and end in
  restores. Each of its refusals was neutralised in turn and the full suite
  re-run — 9 400 tests, seven times — and three of the seven plants passed
  unnoticed:

  - `manager.verify(...)` replaced by `pass`. This is the distinction both the
    prose and `_unprepared`'s docstring draw — a backup that *reads back* rather
    than merely existing — and it was the one refusal in `prepare` with nothing
    behind it. The new test corrupts one backed-up file in place, same length,
    different bytes: the file exists, the listing is unchanged, and only the
    SHA-256 in the manifest disagrees. That is the damage an existence check
    cannot see and the reason `verify` re-hashes.
  - the missing-save-directory refusal. A backup record outlives the save it was
    taken from, so a world renamed after the backup leaves a machine where the
    backup verifies perfectly and the save it describes is not there. Without
    the refusal `prepare` writes `ready`, `run` unlocks, and the record names a
    backup as if it covered a save id that resolves to nothing.
  - the no-Zomboid-directory refusal. Neutralised, the next line raises
    `TypeError: unsupported operand type(s) for /: 'NoneType' and 'str'` — an
    operator whose game was never found where discovery looks would meet a
    traceback instead of the refusal that names `pz-agent doctor`.

  No product behaviour changed: all three refusals were correct and unguarded,
  which is a defect in the suite rather than in the CLI. `tests/contract/`
  `test_operator_loop.py` now drives each through the real CLI over a synthetic
  Zomboid directory, and each plant fails one of them.

  The other four were already guarded — the missing schema, the absent `--save`,
  the save name without "test", and the absence of any backup — so this is
  measured coverage of the subcommand's refusals, not a claim about the rest.

- **The instruction that keeps a live session from having to be repeated existed
  in one document of three, and nothing checked it there either** (`dev`).
  `finalize` requires the declared logs of every scenario, passes included, and
  those files do not survive the day: `console.txt` is rewritten each time the
  game launches and the session trace rotates, so logs gathered in the evening
  are missing the early scenarios' entirely. The only remedy is to run those
  scenarios again.

  Found by planting rather than by reading: cutting section `## 4a` out of
  `LOCAL_AGENT_PROMPT.md` left the entire contract suite green. The playbook and
  the handoff each named `collect-evidence.bat` only in a command table, with no
  word about when to run it — so two of the three documents an operator follows
  never gave the instruction at all.

  All three now show the per-scenario form and say why waiting loses the logs,
  and `tests/contract/test_logs_are_collected_per_scenario.py` holds it over the
  set imported from `test_handoff_instructions_match_the_run.INSTRUCTIONS`. The
  anchor is `collect-evidence.bat --scenario`: mechanical, language-independent
  — one of the three documents is in Russian — and the form the instruction is
  actionable in. It proves each document shows that call, not that it explains
  the reason; anchoring on the explanation would mean inventing a phrase in two
  languages that only the test looks for. A separate check confirms the wrapper
  really accepts the flag, so the three documents cannot agree on a spelling
  that produces a usage error.

  Four plants, four failures: the flag removed from each document in turn, and
  the wrapper's own flag renamed.

  Those four plants were the convenient kind — every occurrence of the string
  replaced — and they flattered the guard. Deleting the *section* instead, which
  is what a rewrite actually does, left it green: `LOCAL_AGENT_PROMPT.md` spells
  the command twice, once in the timing rule and once in "what to do after a
  FAIL", so cutting the timing rule left the other occurrence behind. A sweep
  that deleted each section of the two long documents in turn found 24 of 28
  removable without a single test noticing, `## 4a` among them. The guard now
  also requires the half the command alone cannot carry — that the scenarios
  which *passed* owe their logs too — through a per-document phrase table whose
  completeness is asserted against the derived instruction set rather than
  trusted. Most of the remaining 23 sections are prose that should not be
  pinned to a magic string; that is a judgement recorded here, not a claim of
  coverage.

- **The version warning reached two of the three documents an operator follows,
  and the guard for it was too weak to notice either problem** (`dev`). The
  warning added last time — bump `version.py` before the live run or the
  evidence cannot certify a release — went into `LIVE_TEST_PLAYBOOK.md` and
  `LOCAL_AGENT_PROMPT.md`, the two documents that were open at the time. This
  repository already defines which documents an operator follows as
  instructions, in `test_handoff_instructions_match_the_run.INSTRUCTIONS`, and
  there are three: `LOCAL_GAME_HANDOFF.md` — the one that hands the whole job
  over — was missing. Its only mention of `version.py` was a description of what
  `check.sh` verifies.

  That is the same guard-scoping mistake as the surface-count guard fixed in the
  previous entry: a scope listed beside an existing definition of the same
  scope. The set is now imported from that definition rather than re-listed, so
  the two cannot disagree.

  Planting found a second, worse problem. Cutting the whole warning out of
  `LOCAL_GAME_HANDOFF.md` left the test **green**: it asked only for the
  substrings `version.py` and `re-run`, and both occur in these documents for
  unrelated reasons. The assertions now anchor on `PRODUCT_VERSION` and the
  gate's own remediation phrase "re-run the scenarios" — measured to occur
  exactly once in each of the three documents, and only in this warning.
  Neutralising the warning in any one of them now fails that document's case.

- **Two more documents were still understating the unverified engine surface,
  and my own guard could not see either** (`dev`). The rule is that
  `docs/GAME_API_VERIFICATION.md` states the size and every other document
  points at it. When that was first fixed, the guard was written around the
  three documents the defect had been found in.

  Measured now: ten documents name the inventory, and two of the seven outside
  that list carried stale figures the whole time. `LOCAL_GAME_HANDOFF.md` still
  said "the 52 engine symbols" and "finds six of them" — the original wrong
  numbers, against a real 167 rows and 10 marker lines in 3 files — and
  `LIMITATIONS.md` said "168 symbol rows", the legend-row miscount that was
  corrected in the inventory and never propagated. Both are documents whose job
  is to size the risk before a live session; one understated it threefold.

  Two separate scoping failures, both mine. The document set was *listed*
  instead of derived, so five satellites were never checked; and the pattern
  required the noun immediately after the number, so "52 engine symbols" walked
  through even once the document was in scope — proved by planting it, which is
  how the second failure surfaced at all.

  Both are fixed at the fact rather than at the files: the satellite set is now
  derived from "names the inventory", with `PROGRESS.md` exempt by name because
  it is the record of this defect and has to be able to quote the wrong figures;
  and the pattern allows one qualifier between the number and the noun. Nine
  satellites are now checked, with no false accusation among them, and both
  stale sentences fail the guard when planted back.

- **A live session run against this tree would have produced evidence the
  release bar refuses, and nothing said so beforehand** (`dev`). Every existing
  test of `scripts/check_release.py` asserts that some particular check is
  absent from the failures; none asked whether a complete, correct evidence tree
  *certifies*. It does not.

  Measured by running the real gate over the release tests' own passing fixture:
  fifteen of sixteen checks pass, and the sixteenth is `evidence.version` —
  "the evidence names product version 1.0.0, this checkout declares 0.1.0, and
  the release is v1.0.0". `finalize` stamps `PRODUCT_VERSION` into the manifest,
  the gate requires that number to be the version being released, and
  `PRODUCT_VERSION` is still `0.1.0`. The gate's own remediation is "bump
  version.py … **then re-run the scenarios**" — after twenty-two scenarios, a
  thirty-minute run and a two-hour run, on a machine this repository cannot
  reach.

  The hazard is the ordering, and the documents an operator follows said nothing
  about it: `docs/LIVE_TEST_PLAYBOOK.md` and `docs/LOCAL_AGENT_PROMPT.md` did
  not mention the version at all, and the 84 open tasks say "follow the
  playbook". Both now carry the rule and its cost before the first scenario, and
  `pz-agent live-test prepare` — the one command every live session passes
  through, since `run` refuses without its record — prints the number it will
  stamp.

  Named, not refused: whether this tree should declare the release version is a
  product decision, and a live run made for some other reason is legitimate. For
  the same reason the version was not bumped here.
  `tests/contract/test_the_release_bar_is_reachable.py` pins both halves — that
  the bar is otherwise reachable, so a new check a real run cannot satisfy fails
  here rather than on the operator's machine, and that the warning is in both
  documents. It also fails deliberately once `PRODUCT_VERSION` becomes the
  release version, so the person doing the bump rewrites it rather than leaving
  a green test outliving its reason.

### Added

- **The plan's verify commands are now checked to name things that exist**
  (`dev`). `docs/control/MASTER_PLAN.yaml` puts a `verify_command` on all 484
  tasks — 150 distinct — and 84 of those are the live validation someone runs on
  a Windows machine with the game open. A renamed test file or a removed flag
  would surface there: after a two-hour endurance run, on a machine this
  repository cannot reach, with the session spent. Nothing checked it, and the
  surface moves in almost every commit that adds a test.

  Measured over the tree, all 150 resolve: every pytest target, script,
  document and grep path is on disk, all 33 `pz-agent` lines parse against the
  real parser with their flags, and the 22 scenario ids the plan names are
  exactly the 22 the catalogue defines, in both directions. So
  `tests/contract/test_the_plan_names_things_that_exist.py` is a guard over a
  correct surface rather than a fix.

  The CLI lines go through `build_parser()` instead of being compared against a
  list of names, because that is the operator's real question — would this line
  run — and it catches a removed flag, which a name comparison cannot. Every
  command must also be *classified*: an unrecognised shape fails rather than
  being skipped, since a classifier that ignores what it cannot parse reports a
  clean plan for a broken one. That check caught the first version of this file
  dropping eight `grep` commands whose paths were globs or bare directory
  names.

  Planted and confirmed: a test file renamed on disk fails by name and command;
  a removed flag, a deleted script and a glob matching nothing each fail their
  own checker.

- **The requirement baseline is now held in place** (`dev`).
  `docs/blueprint/` is marked read-only in the repository map: it is what every
  claim of conformance is measured against. A yardstick that can be adjusted to
  match the thing being measured is not a yardstick, and the adjustment is the
  kind nobody notices — a clarified sentence, a scope line softened to match
  what was built. Nothing enforced it.

  Measured: 22 files, introduced by exactly one commit, with zero
  modifications, deletions or renames since. The rule has never been broken, so
  this is a guard over a discipline rather than a fix for a defect.

  `tests/contract/test_the_blueprint_is_the_baseline.py` checks two moments,
  because they catch different things. History — no commit beyond the one that
  created it — is what CI judges, and the failure names the offending commit.
  The working tree catches an edit while it is still uncommitted, which `git
  log` cannot see at all and which is when undoing it costs nothing. Both were
  planted and both fired. A third test pins that the directory still holds its
  22 files, since an emptied baseline would satisfy the other two forever. A
  fourth runs the history query against a file that does change, so a query that
  silently matched nothing could not report every path as pristine.

  On a shallow clone the historical halves skip with that reason rather than
  pass, the same answer `scripts/audit_pass.py` gives.

- **The domain layer's two architectural rules now have a check** (`dev`).
  The repository map's strongest claim — `pz_agent_core` carries zero
  third-party runtime dependencies, no MCP SDK, no LLM SDK, no UI — had nothing
  enforcing it, nor did the layering line that puts core at the bottom.

  Measured, and the measurement changed the rule rather than confirming it.
  Importing all 109 core modules in one process pulls in nothing outside the
  standard library — and that answer is wrong: a static scan of the source finds
  `yaml` in `knowledge/loader.py`, imported inside a function, which never runs
  at import time. It is handled well, and that is why the check now encodes what
  is actually true instead of what the map said. The import sits inside a `try`
  whose `ImportError` handler raises `CorpusError(YAML_UNAVAILABLE)`, which stops
  planning rather than continuing without the rules the user configured, so core
  still runs with nothing installed.

  `tests/contract/test_core_carries_no_dependency.py` therefore allows a
  third-party name only when it is listed with its reason *and* is reached
  through a guard that turns absence into a typed refusal — an allowance that
  did not check the guard would be a way to smuggle in a hard dependency. It
  also holds the layering: core must not name `pz_agent_cli`, `pz_agent_mcp` or
  `pz_agent_voice`. The scan is static because the runtime one cannot see a
  deferred import, which is the shape both real cases use — `yaml` here, and
  `pz_agent_mcp/__main__.py`'s deliberate import of the CLI.

  Three plants, three failures: `import httpx` deferred inside a provider
  method; the `try`/`except ImportError` removed from around `yaml`, turning the
  optional parser into a hard dependency; and `import pz_agent_cli` added to the
  action engine.

  Checked in the same pass and found sound, reported rather than changed:
  `pz-agent-mcp.exe` does not bundle PyYAML and does not need to — the planner
  provider that loads the corpus is built in `pz_agent_cli`, which runs in
  `pz-agent.exe`, and that spec does list `yaml` as a hidden import. The MCP
  process only compacts an observation for the planner.

- **A write to the terminal outside the CLI is now refused, and the MCP
  process's stdout is checked to stay clean across the one boundary it crosses**
  (`dev`). An MCP client launches `pz-agent-mcp` and parses its stdout as
  JSON-RPC, so a stray line is a parse error the client reports as this server
  being broken. The repository map's one line — `pz_agent_cli` is the only
  package allowed to `print` — reads as though package boundaries enforce it.
  They do not.

  Measured: `pz_agent_mcp/__main__.py` imports `pz_agent_cli.context`
  deliberately, so the state directory is derived once instead of by two copies
  that drift, and that import loads 36 CLI modules into the serving process —
  including `pz_agent_cli.output` and `pz_agent_cli.status`, which hold every
  `print` in the repository. `redirect_stdout` in that module wraps only
  argparse; the serving path writes to the real descriptor, as it must. Nothing
  on the crossed path prints today — the import and both calls put zero bytes on
  stdout in a real child process — and nothing but discipline kept it that way.

  Two checks, complementary by design. `scripts/check_forbidden.py` gains a
  `terminal-write` rule refusing `print` and `<stream>.write` outside the CLI;
  writing to a stream the *caller* named is the sanctioned pattern in
  `__main__.py` and is untouched. `tests/contract/test_mcp_stdout_belongs_to_the_protocol.py`
  crosses the boundary in a real subprocess and reads the pipe a client would
  parse.

  Each catches what the other cannot, shown by planting: a `print` in
  `pz_agent_core`, in `pz_agent_mcp`, in `pz_agent_voice`, and a
  `sys.stdout.write` in the entry point are all caught by the static rule; a
  `print` added to `pz_agent_cli.context` is invisible to it — printing is what
  the CLI is for — and fails the subprocess test instead.

### Fixed

- **The command sink's progress table grew without bound for the life of a
  session** (`dev`). `QueueCommandSink` records when each shipped command was
  last heard from, so the reflex guard can tell a command that is working from
  one that has stopped. Entries went in on every send and every ack, and came
  out on exactly one event — a terminal ack — so a command that never got one
  stayed forever.

  The queue guarantees there are such commands. `CommandQueue._track` sheds the
  oldest evictable entry once `pending_limit` is reached, and a shed command's
  terminal ack is filed against nothing. Measured with the real classes at
  `pending_limit=8`: two hundred accepted commands left the queue tracking eight
  and the sink holding two hundred — 192 entries for commands the queue had
  already forgotten, which nothing could read and no ack could remove. AGENTS.md
  requires bounded memory and calls anything unbounded a bug.

  The sink now prunes to the queue's own pending set rather than to a second
  limit of its own, because a duplicate bound is one that drifts. Nothing is
  lost: `AgentRuntime._in_flight` is the only caller of `last_progress_ms` and
  iterates exactly that set, and the in-flight command is the one entry
  `_evictable_command_id` refuses to shed. `progress_command_ids` publishes the
  table's keys so the bound is asserted against the queue instead of by reaching
  into a private attribute.

  `tests/unit/test_sink_progress_is_bounded.py` fails in both directions:
  removing the pruning restores the growth, and pruning greedily (clearing the
  table) drops the entry the guard actually reads.

### Added

- **A state-changing engine call can no longer enter the mod unnamed** (`dev`).
  AGENTS.md ends its capability-honesty rule with "never simulate the effect by
  writing stats", and nothing enforced it: `scripts/check_forbidden.py` reads
  shipped Lua only for stub markers and dynamic loading, and
  `docs/GAME_API_VERIFICATION.md` records what the mod calls without anything
  comparing it against the code.

  That is the one point where "success only by observation" fails silently.
  Every engine access goes through `Toolkit.call(owner, name, ...)`, a generic
  dispatcher with varargs that writes as readily as it reads, and
  `ActionRuntime.verify` asks only that some `x_before` differ from its
  `x_after` — both readings taken by the adapter itself. So an adapter that set
  the player's endurance would return a `succeeded` ack carrying evidence of a
  change it caused, and mutate the save.

  `tests/contract/test_state_changing_calls_are_declared.py` requires every
  state-changing name spelled in shipped Lua to carry a row in the inventory.
  Measured over the tree, five such names exist — `setForceShove`, `setDoShove`,
  `DoShove`, `pressAttack`, `DoAttack`, the two input-press tables in
  `Combat.lua` — and all five are documented, so nothing correct is accused. A
  planted `Toolkit.call(stats, "setEndurance", 1.0)` in `Rest.lua` fails the new
  test by name and line, while `check_forbidden.py` still reports "No forbidden
  patterns found" — which is what the hole looked like.

  It deliberately does not judge whether a mutating call fabricates an effect;
  no scanner can, and that stays a review question. It makes the act impossible
  to perform quietly.

  Audited alongside and found sound, reported rather than changed: the engine
  method-name surface is closed — all 74 dispatcher call sites take either a
  string literal or an entry of a module-level literal table, so no name can
  come from a command or the model; and the inventory covers every name the code
  reaches. That last one took three attempts at the comparison, and every
  apparent gap (39, then 11, then 2) was the parser reading one side too
  narrowly, not a missing row.

### Fixed

- **The local gate no longer claims success without saying which tree it
  judged** (`dev`). `scripts/check.sh` ended with a bare `All checks passed.`
  and its header claimed "CI runs exactly these steps, in this order, so a green
  run here means a green run there". CI judges a commit; this script judges
  whatever is on disk. Those are the same thing only when the tree is clean, and
  the unqualified line said nothing about which case it was.

  That produced a real red build, not a hypothetical one. On 2026-08-16 the gate
  printed the success line over a working tree with uncommitted work, the commit
  made from that tree was pushed, and CI went red on `c4b08ef`: at that commit
  `docs/control/STATUS.json` still described the previous one, which
  `scripts/check_master_plan.py` refuses. Reproduced from the commit itself, not
  inferred from the log.

  `scripts/check_tree_identity.py` now names the subject of the verdict, before
  the run and after it, in three states and only three: a clean tree (the
  verdict is about that commit, which is what CI will judge); a tree differing
  only inside `docs/control/` (the prescribed state between the code commit and
  the STATUS commit, whose tree is this one, so the verdict carries); anything
  else (the verdict is about no commit). It never fails the gate — running the
  checks over uncommitted work is ordinary, and a checker that refuses ordinary
  work gets switched off. Calling such a run a verdict about a commit was the
  defect.

  Measured while fixing it: every code commit in this repository is inadmissible
  to its own gate, because a commit cannot carry a STATUS describing itself —
  12 of the last 25 commits on `dev`, exactly the code ones. That is harmless
  while the code commit and its STATUS commit are pushed together and permanent
  the moment they are not, so AGENTS.md now says the pair is one unit.

  A test caught a defect in the fix itself: `git status --porcelain` collapses a
  wholly untracked directory to the directory, so a new file under
  `docs/control/` printed as `?? docs/` and the prescribed state was classified
  as a verdict about no commit. `--untracked-files=all` reports each path.

### Added

- **A mod that claims success without evidence is now pinned to be quoted rather
  than silenced** (`dev`). `ActionResult.succeeded()` refuses to build a success
  ack without observed evidence; `ActionResult.from_dict` does not, and the mod's
  `Handle:ack` writes the `evidence` key only when the bag has entries — so the
  shape can arrive over the wire.

  Every consumer was checked, not assumed, and every one is defended: the engine
  treats a `succeeded` ack as a little more grace and never as the answer, ending
  at `POSTCONDITION_FAILED` ("the mod reported success but the postcondition was
  never observed"); `_sink_refusal` refuses a replayed success; the MCP
  `ActionRecord` refuses the combination; arming needs the game's own heartbeat
  besides the ack.

  The obvious hardening — move the check into `__post_init__` — is a regression,
  measured by planting it: the claim is dropped as an unusable ack and the answer
  degrades to `ACTION_TIMEOUT`, which reports that the mod went quiet when in fact
  it lied. A decoder must be able to transcribe a claim it cannot verify, or the
  receiver has nothing to name.

  Nothing pinned that: the plant left `test_actions_engine.py` and
  `test_ipc_queue.py` fully green, both building their acks through the
  constructor that carries evidence.
  `tests/unit/test_a_success_claim_without_evidence_is_named.py` covers the path
  an ack really travels — journal bytes, decoder, engine — and AGENTS.md now says
  which half of the honest-state rule binds the decoder. No defect found in the
  shipped behaviour; the guard is over a decision that had none.


- **Game-authored text is now checked to reach the model only under the key that
  labels it** (`dev`). AGENTS.md: "All in-game text … is untrusted data, never
  instructions." `observation/compact.py` implements that two ways and both are
  sound — free text is nested under `untrusted_text` beside a `content_rule`
  that says what it is, and every other string goes through `_token`, which
  keeps identifier-shaped values and drops the rest.

  What nothing checked was the **enumeration**. Two call sites wrap text today;
  a field added later — a sign's legend, a radio transcript, a server's message
  of the day — carrying free text straight into the document would reach the
  planner unlabelled, and existing coverage would not notice: it asserts that
  the wrapped fields are wrapped, never that there is no third path.

  `tests/unit/test_game_text_is_labelled.py` sets every free-text field to a
  sentinel, runs the real `compact_for_planner`, and walks the document: a
  sentinel may appear only inside a mapping reached through
  `UNTRUSTED_TEXT_KEY`. Planted a new unlabelled field and it fired.

  The sentinel is deliberately not identifier-shaped, and there is a test that
  it reaches the document at all — a sentinel `_token` would drop could vanish
  and leave the whole file passing over nothing, which is the failure mode of
  every "nothing bad appears" assertion.


- **Every `SuccessKind` is now classified, so a new one cannot reopen the
  invented-reference hole quietly** (`dev`). The critic gates `ITEM_CONSUMED`
  because that criterion is satisfied by the item's *absence*; closing that
  instance is not closing the class. A seventh kind phrased as a disappearance —
  `item_dropped`, `container_emptied` — would be satisfied the same way and
  would not be in `_ITEM_READING_CRITERIA`.

  `tests/unit/test_success_kinds_are_classified.py` does not compare two lists.
  For every kind the enum declares it runs the real `SuccessCriterion.holds`
  twice — once with a reference the observation carries, once with one it does
  not — and requires that a kind answering **True only for the unobserved
  reference** is gated at the critic. That cannot be satisfied by editing a
  literal.

  Which kinds are in scope is measured rather than declared, and that mattered:
  the first version declared the scope by hand and accused `position_reached`,
  which reads the player's position and no reference at all. It was a false
  accusation of exactly the kind this repository has been taught to avoid, and
  the fix was to let the measurement decide — a kind is reference-reading when
  its answer changes with the reference it is given.

### Fixed

- **A reference the model invented produced a finished plan and a silent
  nothing** (`dev`). AGENTS.md: the LLM "may emit a typed plan and nothing
  else … no raw refs it invented". The plan type enforces most of that
  structurally, and the critic's `_foreign_ref` catches a reference minted by
  another session. A well-formed reference from *this* session naming an item
  nobody ever observed was approved.

  Measured end to end:

  1. `Plan.from_payload` accepts it — it is syntactically valid.
  2. `PlanCritic.review` **approves** it, with the same verdict as a real one.
  3. `SuccessKind.ITEM_CONSUMED` asks whether the item is *gone*. An item that
     never existed is gone, so `success.holds` returns **True**.
  4. `PlanExecutor._gate` checks `success.holds` **before** `_ref_gate` — the
     check that would have refused it with `INVALID_REF` — so the step is
     **skipped as already satisfied** and the gate is never reached.
  5. The run ends `COMPLETED`, every step "accounted for", nothing sent to the
     game, and the character never ate.

  The critic now refuses at review, under a new `UNOBSERVED_REF` rule. Narrow on
  purpose: only criteria whose *satisfaction is the referent's absence* are
  gated, and only the item is looked up. A step may legitimately name a
  reference the current observation does not carry — a later step acting on
  what an earlier one reveals — and the executor re-checks against a fresh
  observation at the step itself, which is where that belongs. A gap in
  observation is likewise not evidence the item is gone, and is not refused.

  **Two existing tests asserted the defect, and both were changed.**
  `test_an_item_nobody_can_find_still_counts_as_consumed` said so in its name;
  its own class docstring already warned that a criterion looser than the
  adapter's postcondition "is a report of a state nobody observed" — that test
  was the report. It now asserts the refusal, and records what it used to
  assert. A recovery test used an empty inventory to make its *replacement* plan
  skip; its subject is the report's wording, so it now replans into a step that
  is genuinely runnable in that world. Changing a tested decision is worth
  naming plainly: the behaviour was deliberate-looking and the measurement is
  what changed my mind.

- **The release gate accepted one statement from the document it exists to
  doubt** (`dev`). `check_release.py` opens with the rule the whole file is
  built on: *"A claim is checked against the artefact, never accepted from it."*
  It re-hashes every artefact the evidence manifest records — that rule,
  correctly applied to the contents. The **path** was taken at face value:
  `evidence_root / path`, with no containment check.

  Measured, both of these returned *verified* with no problem reported:

  | recorded path | what the join produced |
  | --- | --- |
  | `../outside.txt` | walks up and out of the evidence tree |
  | `/anywhere/outside.txt` | pathlib **replaces** the root on an absolute operand, so the evidence root vanishes from the expression |

  The second needs no traversal at all. `..\outside.txt` is the Windows half,
  invisible here for the same reason yesterday's installer hole was.

  What that buys a wrong manifest is a **green** `evidence.artefacts` —
  *"N required artefact(s), each with a SHA-256; N re-hashed from &lt;root&gt;"* —
  over files that are not the evidence, in the bar that certifies v1.0.0. Not a
  remote attack: the manifest is written by `live-test finalize` and named on
  the command line. But a gate whose stated purpose is to disbelieve a document
  must not take that document's word for where to look.

  An escaping path is now **reported**, not skipped. A skip would record it as
  "not re-hashed", which reads as an absent evidence tree rather than as a
  manifest pointing outside the one it describes — an understatement exactly
  where understating is dangerous. `tests/unit/test_release_gate_artefact_containment.py`
  holds both, plus the control that a wrong digest *inside* the tree still fails
  for the digest rather than the containment.

  Third instance of one class in three days — a Windows path parsed in a test, a
  traversal guard splitting on one separator, and now this. All three are a path
  from a recorded document reaching the filesystem without containment, so the
  fix here is deliberately the installer's idiom rather than a new one.

- **The installer's traversal guard refused one separator convention and shipped
  to the other** (`dev`). `modinstall._check_relative` states its purpose —
  *"refusing anything that could escape the mod directory"* — and split the path
  on `/` alone:

  | path | verdict |
  | --- | --- |
  | `../../evil.txt` | refused: `..` becomes a part |
  | `..\..\evil.txt` | **admitted**: nothing splits it |

  Measured, not reasoned about. The backslash string stays one segment, that
  segment is not `..`, the depth is 1, so every check passes; on Windows
  `destination.joinpath` then reads the backslashes as separators and lands
  outside the mod directory.

  Stated precisely rather than dramatised: this is not a remote attack. The
  paths come from the install ledger `pz-agent` itself writes, and it writes
  POSIX. But that ledger lives in the user's Zomboid directory, is read back on
  the next `install-mod`, and every path in it goes through this guard — after
  which the entries the audit calls stale are `unlink`ed. A corrupt or
  hand-edited ledger could therefore delete a file this project never installed,
  which is the outcome the surrounding audit exists to prevent: it raises
  `ForeignFileError` on the first file pz-agent did not write. AGENTS.md ranks
  user safety first, and this is on the platform the product ships to.

  The fix is `platform/backup.py`'s existing idiom — normalise `\` to `/` before
  splitting — applied where it was missing rather than invented. One call site
  in `_audit_destination` split a manifest path itself instead of going through
  the guard; it now goes through it.

  `tests/unit/test_install_path_traversal.py` builds the Windows shape with
  `PureWindowsPath`, per D-004, so the defect fails on any platform instead of
  waiting for a Windows run. It separates escapes from dot segments — those are
  refused for tidiness, not because they escape, and merging the two would have
  made the consequence test assert something untrue of half its cases — and
  carries the control that legitimate install paths still work.

- **The check that found it was itself the previous commit's Windows failure.**
  See below: `test_unverified_surface_is_counted` shelled out to grep and
  recovered filenames with `split(":", 1)[0]`, which returns the drive letter on
  a `D:\a\...` runner. Both defects are the same class, one in a test and one in
  the installer, found within a day of each other.

- **The stale scenario range was in six more places, and my own guards were the
  reason nobody saw them** (`dev`). Two earlier iterations wrote checks for
  `S01..S20` — one scoped to `packaging/windows/bat/*.bat`, one to three named
  handoff documents. Both were drawn around the file where the defect had just
  been caught rather than around the fact, which is the mistake this repository
  keeps documenting about other people's code.

  A tree-wide sweep found: `scripts/check_release.py`'s own module docstring, a
  description in `schemas/gameplay-knowledge.schema.json`, a task string in
  `scripts/plan_epics_d.py` (and therefore a `pass_criterion` in the generated
  `MASTER_PLAN.yaml`), a status row in `docs/PROGRESS.md`, and two claims in
  `docs/RELEASE.md` — the release procedure itself.

  Stated plainly: **none of them was reachable.** The release gate calls
  `_scenario_ids()`, which imports `SCENARIO_IDS`; the schema constrains
  `proven_by` by length only. They were prose. What they cost is a reader of the
  file that decides whether v1.0.0 ships being told the catalogue ends two
  scenarios early.

  The two narrow checks are removed and replaced by
  `tests/contract/test_scenario_ranges_match_the_catalogue.py`, which sweeps
  every `.py`, `.md`, `.json`, `.yaml`, `.bat` and `.lua` in the tree.

  **Two catalogues exist and their numbers collide** — `docs/RELEASE.md` says so
  in as many words — so `S01–S15` there is *correct*: it describes
  `tests/game-smoke/`, not the live-test catalogue. A checker that flagged it
  would have been a false accusation, the failure this project has already been
  taught once. Both ends are derived instead: the live catalogue from
  `SCENARIO_IDS`, the smoke catalogue by listing its directory. A range is wrong
  only when it ends at neither, and a further test asserts the two really do end
  differently, so the rule cannot loosen unnoticed.

  `MASTER_PLAN.yaml` was regenerated rather than hand-edited — one line changed,
  every status and commit preserved, weighted progress unmoved at 73.3%.

### Verified, unchanged

- **`docs/LOCAL_DEBUG_MAP.md` checks out.** The last handoff document never
  examined for content: every Lua and Python module its triage table names
  exists, all seven reason codes it tells the agent to look for
  (`CAPABILITY_UNAVAILABLE`, `INVALID_ARGUMENT`, `INVALID_REF`, `LEASE_EXPIRED`,
  `PATH_STUCK`, `PLAYER_BUSY_MANUAL_ACTION`, `SEQ_CONFLICT`) are emitted
  somewhere in the tree, and all eleven exchange-directory filenames it lists
  appear in the code that writes them. Nothing to fix; recorded so the next
  sweep does not repeat it.

- **Four documents carried four different sizes of the unverified engine
  surface, and none of them was the tree's** (`dev`).
  `docs/GAME_API_VERIFICATION.md` is the inventory of every Project Zomboid
  symbol this mod touches without ever having called one. Its size is the single
  number that tells the agent about to run the first live session how much risk
  is in front of them.

  | document | `grep "Build 42:"` | `requires_live` rows |
  | --- | --- | --- |
  | `GAME_API_VERIFICATION.md` | "nine lines, in two files" | "159 symbol rows" |
  | `LOCAL_DEBUG_MAP.md` | "six comments" | "52 symbols" |
  | `LIVE_TEST_PLAYBOOK.md` | "finds six of them" | "52 symbols" |
  | `LOCAL_AGENT_PROMPT.md` | "шесть строк в двух файлах" | "сто двадцать четыре" |
  | **measured** | **10 lines, 3 files** | **167 rows** |

  Two of them said fifty-two against a real one hundred and sixty-seven — the
  unverified surface presented as under a third of its size, in the documents
  whose entire job is to size it before anyone starts. The inventory itself, the
  one that says *"This document is the list"*, was wrong about its own table.

  The number now lives in exactly one document and the other three point at it.
  That is the shape used for the wrapper counts in the previous entry, with one
  difference worth stating: a wrapper can simply drop its count because nobody
  needs it there, while this figure **is** the point — so it is stated, and
  checked. `tests/contract/test_unverified_surface_is_counted.py` runs the grep
  the documents tell the operator to run and parses the table as a table, so
  adding a symbol row or a `-- Build 42:` comment fails the suite until the
  inventory's own sentence is updated. Deliberate: the alternative is what this
  replaces, four copies drifting at once.

  It also pins the direction nobody was watching — the sentence claims *every*
  row is `requires_live`, so a row confirmed by a live session must move that
  wording rather than quietly turn it into an overstatement.

- **Seven scenarios owe a screenshot, and nothing told the operator to take
  one** (`dev`). The sibling of the log defect below, and worse in the way that
  counts. What the playbook said, in full, was `— screenshots required`
  appended to the **Evidence** line of seven sections. Not the directory, not
  when to take it, not that `finalize` refuses without it.
  `LOCAL_AGENT_PROMPT.md`, `LOCAL_GAME_HANDOFF.md` and `LOCAL_DEBUG_MAP.md`
  mentioned screenshots **zero times** between them.

  Measured: `S11_CONTAINER` driven to PASS through the real runner, every
  declared log written, and the real `finalize` still refuses — *"no screenshot
  was collected for a scenario that requires one"*.

  A log survives on disk until the next game launch, so a late `collect`
  recovers some. A screenshot is a moment in a running game, and **no command
  produces one**: `live-test collect` gathers logs and journals and never
  touches that directory — asserted against the source, so if it ever learns to,
  the playbook's claim gets revisited rather than quietly becoming false. The
  moment is over when the scenario ends.

  The generator now prints, under each requiring scenario, the directory —
  composed from `SCREENSHOTS_DIR_NAME`, the same constant
  `EvidenceLayout.screenshots_dir` uses, so a rename moves the instruction with
  the code — the moment to take it, and the refusal it avoids. It also says
  plainly that the runner checks only that a file exists: whether the shot shows
  what the postconditions describe is the operator's judgement, which is the
  entire reason the scenario asks for one. The prompt gains §4b.

  `tests/contract/test_screenshots_are_asked_for_in_time.py` holds both
  directions — an instruction wherever the catalogue requires a screenshot and
  nowhere else — on top of the measured refusal and its control.

- **The prompt handed to the local agent told it to collect logs at the end of
  the run, and the logs do not survive that long** (`dev`).
  `docs/LOCAL_AGENT_PROMPT.md` is not a reference — it is the text pasted into a
  fresh session on the machine with the game, followed step by step. §5
  collected evidence *after a FAIL*, and §7 step 4 said `collect-evidence.bat`
  once everything had passed.

  Measured against the real runner and the real `finalize`: a scenario driven to
  **PASS** with no logs collected is refused, naming five missing files —
  `console.txt`, `pz-agent.log`, and the three journals. `_audit_one` marks every
  name in `scenario.logs` `required=True` with no reference to the verdict, and
  all twenty-two scenarios declare some. So an operator whose run went well the
  first time collects nothing and meets that refusal twenty-two times over.

  And it does not repair. `console.txt` is rewritten on every game launch —
  `collect-evidence.bat`'s own header says so — and the session trace rotates.
  By the end of a day the early scenarios' logs are gone; the only remedy is to
  play them again. This is the first of these findings whose cost cannot be paid
  back from a file the operator still has.

  `docs/LOCAL_GAME_HANDOFF.md` did carry the right rule — *"run `live-test
  collect` at the end of each scenario rather than at the end of the day"* — six
  hundred lines in, inside a paragraph about trace rotation, justified by the
  trace rather than by the refusal. Two documents in one handoff bundle
  disagreed about the order of operations, and the one written as instructions
  had it wrong. The prompt now carries §4a, with the reason and the file name.

- **The same prompt was two scenarios short, and its sibling was not** (`dev`).
  It said *"двадцать сценариев S01-S20"* and headed its final section *"После
  того как все двадцать сценариев PASS"*, against a catalogue of twenty-two
  ending at `S22_BUILD` — while `LOCAL_GAME_HANDOFF.md` says twenty-two
  correctly, twice.

  The rule for prose is not the rule for a `.bat`. A wrapper cannot import
  anything, so the previous entry removed its counts outright; a document may
  reasonably state one, and the handoff does it correctly. So
  `tests/contract/test_handoff_instructions_match_the_run.py` asserts that a
  count, **where stated**, equals the catalogue's — in English and Russian, since
  the bundle is written in both — and that a range ends where the catalogue does.
  `CHANGELOG.md` and `FINAL_IMPLEMENTATION_REPORT.md` are excluded by name: they
  record what was true when a defect was found, and editing that to today's
  number would falsify it.

  The prompt's §4 also showed `run-live-tests.bat` bare, repeating the omission
  fixed in the wrappers last commit; it now leads with the `--scenario` +
  `--observations` pair, the only form that can produce a PASS.

### Added

- **The three evidence checks added after the manifest round-trip test now cross
  that seam too** (`dev`). `evidence.commit`, `evidence.game_build` and
  `evidence.components` were each tested only against hand-built manifests —
  the one-sided shape `tests/contract/test_evidence_manifest_round_trip.py`
  exists to prevent. They are now run over a manifest `finalize` really wrote,
  together with an assertion that the runner writes each key they read, since
  two of the three reach their verdict through `.get` and would report a passing
  check over an absent key. Measured: all three already agree. Recorded as a
  tightening, not a defect found.

- **The wrapper the operator double-clicks described a catalogue two scenarios
  short, and advertised the one flag combination that cannot work** (`dev`).
  `packaging/windows/bat/` is the entire interface of the release: nobody on the
  Windows machine types `pz-agent`, they run `run-live-tests.bat`. Its `rem`
  block is their manual, and two claims in it were false.

  `run-live-tests.bat` opened with *"Run the twenty live scenarios, S01 to
  S20"* and `finalize-release.bat` with *"only when all twenty scenarios are
  PASS"*. The catalogue holds twenty-two, `S01_INSTALL` through `S22_BUILD`.
  The two an operator would have dropped are the craft and the placement — the
  only irreversible ones, and the two the playbook's own preamble singles out.

  The same file advertised `run-live-tests.bat --observations obs.json`.
  Measured rather than read: that line exits 1 with *"--observations describes
  one scenario, but 22 were selected"*. `--observations` describes one scenario
  and must be paired with `--scenario` — and that pair is the **only** form that
  can produce a PASS, since a run with nothing to observe records BLOCKED. So
  the wrapper advertised the combination that never passes and omitted the one
  that does. The playbook's hand-written "Running them" block had the same gap
  and now leads with the working form.

  The fix is not "say twenty-two". That is a fresher literal waiting to rot in a
  file that cannot import anything, and this is the **third** stale scenario
  count found here — after `LIVE SCENARIOS: 0/20` in the retired progress
  reporter and "twenty-two" spelled into an error message in `scenarios.py`. The
  wrappers now state no count at all and point at the generated playbook, and
  `tests/contract/test_wrapper_comments_match_the_catalogue.py` holds that:
  every scenario id a wrapper names must exist, no wrapper may state a count or
  a range endpoint, and an `--observations` example's own tokens go through the
  real `resolve` and must select exactly one scenario.

- **Three scenarios declared that they measure latency, and none of them did**
  (`dev`). `measures_latency` is set by `S04_MOVE`, `S19_AUTONOMOUS_30_MIN` and
  `S20_AUTONOMOUS_2_HOURS`, and it was read by exactly two things, both of which
  only *described* it: `generate_playbook.py`, which prints **latency measured**
  (p50/p95 recorded in `result.json`) under each of those sections, and
  `latency_summary`, which writes `"measured": false, "samples": 0` when no
  samples were supplied.

  So the shipped document promised a measurement, the evidence recorded that
  none was made, and the verdict said `PASS`. That combination is what separates
  this from the `game_build` entry below: there the release gate caught it, late
  and expensively. **Here nothing would have caught it at all** — `finalize`
  does not read the latency block and neither does `check_release.py`. The
  promise would have shipped as evidence of a measurement nobody made.

  The playbook made it certain rather than merely possible: the observations
  skeleton had no `latencies_ms` field, so an operator following the document
  supplied none by construction.

  `decide` now refuses a PASS for a scenario that declares a measurement and
  carries no samples, under a new code `LATENCY_NOT_MEASURED`; the two evidence
  rules live in one `unmet_evidence` function so the verdict and the code it is
  reported under cannot come apart. The skeleton carries `latencies_ms` for
  exactly those three scenarios, and — the half that matters as much as the
  refusal — the prose names a real source: `pz-agent latency --json` publishes
  one entry per command in `traces`, each with `issued_at_ms` and
  `terminal_at_ms`, and the sample is their difference. A required field with no
  honest source would push an operator toward plausible invented numbers, which
  is worse than the empty list it replaces.

  The result schema gains the other direction of its latency rule — `measured:
  true` requires at least one sample and non-null percentiles. Stated as what it
  is: a tightening, not a reachable defect, since `latency_summary` sets
  `measured` only from a non-empty list. It refuses a document assembled by any
  other route.

  Held by `tests/unit/test_latency_scenarios_measure_latency.py`, including the
  control that the nineteen scenarios which declare nothing are *not* held to
  it — a rule strict in the wrong places gets switched off wholesale the first
  time it blocks something legitimate.

- **A scenario could PASS without naming the game it passed against** (`dev`).
  `game_build` is the one field a live result carries that no postcondition
  covers. Twenty-one of the twenty-two scenarios say nothing about the build,
  and the twenty-second, `S01_INSTALL`, has a `build_string` postcondition that
  reads `observations.game.build` — a *different* value from the top-level
  `game_build` the result records and the manifest gathers. So every scenario,
  S01 included, reached `PASS` with the top-level build unread.

  What that costs is not hypothetical. `build_result` writes `(not observed)`,
  `finalize` gathers it into the manifest's `game_builds`, and
  `check_release.py --rc` refuses the archive — *"which is the runner saying
  nobody looked"*. That refusal is right. Its **timing** was the defect: it
  arrives after all twenty-two live sessions have been spent, on a machine this
  project does not have, over a string the operator could have typed at the
  first scenario from a session they still had open.

  The playbook made it likely rather than merely possible: the skeleton added in
  the entry below emits `"game_build": ""` and its instruction read *"fill every
  `null`"*, which names every field except this one.

  Two refusals now, because they fail at different points. `decide` refuses the
  verdict — a run whose every postcondition held but whose build is blank is
  `FAIL` with a new code, `BUILD_NOT_OBSERVED`, so an operator is told at the
  scenario and not sent to inspect a game that behaved correctly. The result
  schema refuses the document under its PASS branch, so a bundle assembled by
  any other route is refused too. The generator names the field in the prose.

  `tests/unit/test_pass_names_its_game_build.py` builds, for each of the 22
  scenarios, a run that satisfies every postcondition — derived from the
  postconditions themselves, with a control asserting they do hold, so the file
  cannot go vacuous — and asserts PASS with the build and FAIL without it. The
  schema half runs the real validator over a real `build_result` document, and
  the last test holds the schema's literal marker against `UNOBSERVED_BUILD`,
  since JSON Schema cannot import a constant and an unwatched duplicate is the
  drift this repository keeps finding.

- **The playbook asked the operator for a file it never described** (`dev`).
  `docs/LIVE_TEST_PLAYBOOK.md` instructs `--observations <file>` for each of the
  22 scenarios. It published every postcondition's key and its prose — "the
  player is standing within a tile of the target" — and dropped the one thing
  the runner uses: the `field`. The catalogue's 83 postconditions read 66
  distinct dotted paths, and those paths existed nowhere but `scenarios.py`.
  `grep "field" docs/LIVE_TEST_PLAYBOOK.md` returned nothing.

  So an operator working only from the document they were handed could not write
  a file the runner would read. A wrong guess is not a soft failure either: the
  path is absent, the postcondition is unread, and the scenario fails —
  correctly, and with nothing to act on. This is the last thing between the RC
  and the 84 `owner: local` tasks, and it is a documentation defect, not a code
  one; every check involved behaves exactly as designed.

  `generate_playbook.py` now prints each postcondition's path and check beside
  the statement — ``` `observations.arrived_at_target` · TRUE ``` — and emits a
  per-scenario JSON skeleton, nested, with every value `null`, carrying exactly
  the fields that scenario's postconditions read and no others. `null` is a form
  to complete rather than a value to accept: every check refuses an unread
  reading, so handing the skeleton back untouched fails, which is the intent.

  Proved by running the producer rather than by matching over it — the lesson
  this project has now been taught twice. `tests/contract/test_playbook_observations_skeleton.py`
  lifts the JSON back out of the published markdown, feeds it through the real
  `parse_observations`, and asks `read_field` — the reader `evaluate` itself
  calls — whether each postcondition's path is present, for all 22 scenarios.
  Both directions: a skeleton listing *everything* would pass a presence check
  while sending the operator to record fields nothing reads, so every path in
  the document must also belong to a postcondition.

- **The gate's headline was the one claim derived from neither the artefact nor
  a check of it** (`dev`). `check_release.py` states its rule at the top: *"A
  claim is checked against the artefact, never accepted from it."* Its own
  successful output — `CERTIFIED v1.0.0-rc1` — is built from
  `build_rc.RELEASE_VERSION`, the **checkout's** constant. The archive records
  its own `release_version` and nothing read it, so the gate labelled whatever
  ZIP it was handed with the name the checkout would have given its own build.

  `archive.release` now refuses an archive that declares another release or
  declares none, and the passing case says which release the archive itself
  claims.

  **Scope, stated plainly rather than dressed up as more than it is.**
  `docs/control/DECISIONS.md` D-012 records that the gate runs in the workflow
  that built, so in the real release path the archive's constant and the
  checkout's are the same object moments apart and this check can never fire
  there. It is a tightening, not a reachable false success — unlike the commit,
  build and component-version gaps closed in the preceding entries. What it buys
  is that the headline now agrees with the artefact, and that an archive examined
  outside that workflow is refused rather than relabelled.

  Found by applying the previous entry's method to the other half of the
  release: every key `build_rc.py` writes into the archive manifest, against
  every key the `--rc` path reads. Seven were unread. The first pass scanned the
  whole gate and reported `mod_version` and `product_version` as covered —
  a false positive, since those strings are named by the *evidence* checks added
  a commit earlier, not by the archive path. Re-scoped to the archive functions,
  they are unread there too; they are left alone deliberately, because
  `build_rc` writes them from the same interpreter that assembles the ZIP and
  D-012 puts the gate in that same job. `file_count`, `name` and `summary` are
  likewise not worth a check: the first two are re-derived by the digest pass
  that opens every member, and the third is prose.

  Three tests, both directions. The relabelled archive is built by rewriting the
  manifest inside a **real, complete** one — the hand-made archive used while
  investigating was refused for incompleteness first, which would have made the
  assertion pass for the wrong reason, and the test asserts `archive.complete`
  still passes so the refusal is about the label. Proved by planting: removing
  the check fails all three.

  Consequence worth expecting: the next RC prints `CERTIFIED v1.0.0-rc1: 9
  check(s) passed` where it printed 8. Measured against a complete archive
  rather than assumed. `docs/control/EVIDENCE_INDEX.md` still says 8 and is
  correct for the RC built at `2d3cfea`, which predates this change; it will be
  updated from the observed line when the next archive is built.

- **Two of the five versions the evidence records were read by nobody** (`dev`).
  This changelog opens with the rule: *"Five versions move independently —
  product, protocol, schema, mod and the supported build range."* The evidence
  manifest records three of them, and `check_release.py --release` compared
  exactly one. `_manifest_version` handles `product_version` and the previous
  entry wired up the build range; `mod_version` and `schema_version` were
  written into the evidence and never looked at.

  They are not decoration. `MOD_VERSION` describes the Lua that runs **inside
  the game** and produces every observation these scenarios are judged on, so
  evidence from another mod version is evidence about other in-game code — the
  same argument the commit and the build already make, applied to the component
  that actually did the observing. `SCHEMA_VERSION` describes the shape of the
  observation documents, which postconditions read by dotted path; a schema that
  moved can move a field out from under a check that still finds something
  there. `evidence.components` now compares both against this checkout.

  Found by **enumerating** rather than by luck. Two consecutive fixes had the
  same shape — a fact the runner records that the gate prints but never checks —
  so this time every key `finalize` writes was listed against every key the gate
  reads. Eight were unread; the enumeration also says honestly which of them need
  nothing: `scenario_count` is strictly weaker than the per-scenario verdict
  check that already iterates the real catalogue, `generated_at`/`generated_at_ms`
  have nothing to check against offline, and `totals`/`artefact_count`/`bytes`
  are sums over `artefacts`, every one of which the gate already re-hashes.

  `test_every_version_the_manifest_records_is_checked_by_something` keeps the
  enumeration, so the same gap cannot reopen one field at a time. Two things
  worth recording about writing it:

  - its first version looked for `manifest.get("x")` literals and reported
    `mod_version` and `schema_version` as unread by the new check too — they are
    read through a loop over a dict whose *keys* are those strings. That is the
    retraction lesson in miniature: a checker blind to the producer's spelling
    reports a false absence. The checker was widened; the code was not bent into
    the shape the checker expected.
  - the first attempt to prove the guard fires did not fire, and the guard was
    right — the plant had landed in `result.json`, which carries the same
    `"schema_version"` line, because the replacement took the first match.
    Re-planted into the manifest literal, it fails as designed.

  Five tests, both directions: another mod version, another schema version and a
  missing one are each refused by name, this checkout's are accepted, and the
  enumeration holds. Proved by planting: removing the check fails four.

- **The release bar never checked which game the evidence came from** (`dev`).
  The runner states the rule on the constant it records when nobody read a build
  off a running session — *"Not a guess at the supported build: evidence that
  cannot name the game it ran against closes nothing."* Nothing enforced it.
  `game_build` appeared in `check_release.py` exactly once, interpolated into a
  detail line, and no code anywhere compared the evidence's build against
  `SUPPORTED_BUILDS`.

  Both halves are reachable, and measured rather than argued:

  - Twenty-one of the twenty-two scenarios declare no postcondition about the
    build at all, so they reach `PASS` with `game_build` unset and the result
    records `(not observed)`.
  - The twenty-second, `S01_INSTALL`, asks only that `game.build` be `observed`
    — driving the real postcondition, it passes on `"42.20"`, on `"41.78"` and
    on `"banana"`. "A build was recorded" was never the same claim as "a
    supported build was recorded".

  So a full manifest could carry `game_builds: ["(not observed)"]` and this gate
  would print it and certify `v1.0.0`, for a project whose `SUPPORTED_BUILDS` is
  exactly `['42.20']`.

  The new `evidence.game_build` check refuses a manifest that names no build,
  one that names `(not observed)`, and one that names a build outside the
  supported set — saying which it found and which is supported. It imports
  `UNOBSERVED_BUILD` from the runner rather than re-spelling it: a second copy of
  that string is precisely the drift this project keeps finding, and a checker
  that agrees only with its own spelling of a constant checks nothing. A test
  parses the gate's AST and refuses a second *value* while leaving the docstring
  prose that quotes it — which caught the first, too-blunt version of that
  assertion.

  Five tests, both directions: unobserved, unsupported and empty are each refused
  by name, `42.20` is accepted, and the marker is pinned to the runner's.
  Proved by planting: removing the check fails four, re-spelling the constant
  fails the fifth.

- **The release bar could certify `v1.0.0` on evidence from other code** (`dev`).
  `check_release.py --release` is the last gate before a tag. It checks that the
  evidence's *product version* matches the release, and its own remediation
  states the principle: *"evidence from a different build is evidence about that
  build."* But `PRODUCT_VERSION` is a single literal that does not move for
  hundreds of commits, so the version cannot tell one build from another inside
  a release series. The commit can — and nothing compared it. The manifest's own
  `commit` was interpolated into a detail line and compared to nothing, and the
  per-scenario commits were **not in the manifest at all**: `result.json` has
  carried `commit` from the start, and `_scenario_summary` — the function whose
  output becomes the release manifest — dropped it.

  This is reachable by the plan's own design, not by misuse. The ledger derives
  *PASS if any attempt passed*, deliberately, so a re-run cannot erase a real
  result — which means a scenario keeps reporting `PASS` after the code moves.
  And the campaign that produces this evidence is expected to move it: `E14-M04`
  is, in as many words, *"record the game incompatibilities the run finds, fix
  each, re-run every scenario a fix touches"*. The natural end of a week of live
  testing is twenty-two passes spread across several commits, and before this
  nothing would have said so ahead of `CERTIFIED v1.0.0`.

  The manifest now carries each scenario's commit, read through `verify_result`
  so the value comes from bytes checked against the recorded digest — the same
  way `game_builds` was already gathered. `evidence/schema/manifest.schema.json`
  requires it, and rejected the field until it was declared, which is the schema
  working. The new `evidence.commit` check refuses when any scenario's commit
  differs from the one the manifest was generated at, or when any is unrecorded,
  and names the scenarios to re-run. Agreement rather than equality with `HEAD`:
  an operator who runs the catalogue without changing code gets one value
  throughout, so the rule is an instruction and not a wall.

  Three tests, both directions — a scenario that passed against other code is
  refused by name and by commit, a manifest with the field stripped refuses
  rather than passing silently, and evidence all taken at one commit is
  accepted. Proved by planting each half: removing the check fails three,
  removing the manifest field fails the round-trip contract suite outright.

  Checked in the same pass and found sound, so they are recorded rather than
  re-examined next time: a scenario declaring zero postconditions cannot pass
  vacuously (`decide` raises, with a comment saying why), and the evidence
  tamper chain is honestly scoped in its own module — *"a tripwire rather than a
  seal: there is no key, and this project ships no secrets"* — which is the
  correct claim for local evidence rather than an overclaim.

- **A safety postcondition could pass on a character nobody read** (`dev`).
  `livetest/scenarios.py` states the rule the live verdict rests on — *"A
  postcondition can only pass on a value that was observed. There is no check
  that succeeds on an absent field"* — and `runner.evaluate` repeats it: *"There
  is no branch that passes on a missing value."* Ten checks make that claim.
  Driving the real `evaluate` over all ten against every way a value can be
  missing, nine held and `UNCHANGED` did not.

  The snapshot path decided presence by key alone (`found_before and
  found_after`), so a field present in both snapshots carrying `null` — or `""`
  — compared equal to itself and passed. The observation path had always applied
  a second rule, `_is_non_empty`; the two paths had drifted apart.

  `UNCHANGED` is used by exactly one postcondition in the catalogue, and it is
  the worst one it could have been:

  ```
  S05_BLOCKED_PATH · health_unchanged · player.health
  "the character took no damage"
  ```

  A safety statement, in one of the twenty-two scenarios whose `result.json`
  becomes the evidence manifest `check_release.py --release` reads before
  `v1.0.0`. With the mod failing to read the character and publishing `null`,
  the runner would have recorded that the character took no damage from a
  reading nobody took — and the outcome document would have said `present: true`
  about it. The same shape is already in this repository's ledger one layer
  down: *a zombie scan that could not run published an empty list, and the
  danger floor read that as NONE*.

  Fixed by applying the module's own `_is_non_empty` on both paths instead of
  one. Deliberately **not** truthiness: `0`, `0.0` and `False` are readings —
  health at zero is a fact about a character, not a failure to look.

  `tests/unit/test_postcondition_needs_a_reading.py` (53 tests) runs the real
  `evaluate` across every `Check` × every missing shape, and the same number
  again over the readings that must still decide — including the falsy ones, and
  the real `S05_BLOCKED_PATH` postcondition rather than a stand-in. Proved by
  planting both: the pre-fix runner fails seven, and the tempting truthiness fix
  fails exactly the three falsy-but-real readings, which is what that half of
  the file is for.

- **`live-test run --scenario ""` reported success having run nothing** (`dev`).
  The command that produces the evidence for every live task — the 84 the
  project is blocked on — printed

  ```
  nothing to run: every scenario is PASS.
  ```

  and exited **0**, with all twenty-two scenarios `NOT_RUN`. Two false
  statements in one line: none of them was `PASS`, and the reason nothing ran
  was that the selection resolved to nothing, not that the work was done. An
  operator at the game machine has been told their run succeeded. Reproduced end
  to end against a prepared evidence tree before the fix.

  The trigger is ordinary: `--scenario "$SCENARIO"` with the variable unset,
  which is how a scripted live session produces it — and the live session is
  scripted, `docs/LIVE_TEST_PLAYBOOK.md` being generated precisely so the
  operator can work from a list. `collect --scenario ""` had the same shape.

  Root: `resolve()` drops blank tokens — a repeated flag picks up stray
  whitespace, which is right — and then returned an empty tuple when *every*
  token was blank. `_selection` asks `if only:`, true for `[""]`, so an explicit
  request reached the branch meant for "nothing left to do". `resolve` now
  refuses a selection that names nothing and lists the ids that exist; the run's
  empty branch, now reachable only with the flag omitted, says how many
  scenarios it means.

  Fixed alongside because it is the same family and the same file: the unknown
  scenario message said *"the twenty-two are:"* beside the list it was printing.
  The catalogue already grew from twenty to twenty-two once and left written
  counts behind elsewhere — the literal `20` in `reconcile_status.py`, the `/20`
  in `progress_report.py`, both removed for this reason. It counts
  `SCENARIO_IDS` now.

  `tests/contract/test_live_test_selection.py` (15 tests) drives the real CLI
  through `app.main` against a real evidence tree, because what was wrong was
  the exit code and the sentence a person reads. Six cases cover both
  subcommands against three blank forms; four hold the other direction, so that
  refusing everything would not pass — the flag omitted still selects all 22, a
  named id still resolves to exactly it, stray whitespace around a real id still
  works, and an unknown id is still refused by name. Proved by planting: the
  pre-fix `resolve` fails six, the pre-fix message fails one.

  Checked in the same pass and found sound, so it is recorded rather than
  re-examined next time: all five documented `--json` invocations parse against
  the real parser; `live-test status` on a fresh tree does list all 22, as
  `LOCAL_GAME_HANDOFF.md` §204 claims; and the command checker covers 127
  invocations across 19 documents including all three local handoff files.

- **The RC identity document enforced a third of its own rule** (`dev`).
  `docs/control/EVIDENCE_INDEX.md` opens its release-candidate table with the
  standard it holds itself to: *"The digest is the identity: an RC is this
  archive, from this commit, by this run, and a claim about 'the RC' that names
  none of the three is a claim about nothing."* Three things — and only the
  sha256 was ever compared against `STATUS.json`.

  So the index could carry the correct digest beside the **wrong source commit**
  and the **wrong workflow run**, and the entire suite stayed green.
  Demonstrated against the real files before the fix: swapping the commit to a
  previous RC's, then the run id to a previous run's, left `pytest tests/unit
  tests/contract` fully passing both times. STALE IDENTITY, in the one document
  whose subject is identity — and a hand-written table beside a generated
  record, which is precisely the pair that drifts. Five consecutive rebuilds
  updated it by hand.

  `test_the_evidence_index_names_the_same_commit_and_run_as_the_record` now holds
  both remaining fields against `release_candidate.source_commit` and
  `workflow_run`, requires each to be stated exactly once (the property the
  digest row already had), and additionally requires the source commit to resolve
  in this clone and be an ancestor of `HEAD` — a 40-hex string that is not in
  this history names an archive built from another branch, which matching
  `STATUS.json` would not make true. Proved by planting all three: a real but
  wrong commit, a real but wrong run, and a well-formed sha that resolves to
  nothing.

  Stated rather than implied: the artefact id in the `archive` row is still
  unchecked, because `STATUS.json` records no artefact id and inventing a second
  source for it would be a check agreeing with itself.

  Checked in the same pass and found sound, so it is on the record: the
  `--release` bar — the gate between here and a `v1.0.0` tag — is covered by 23
  tests in `tests/unit/test_check_release.py`, a dozen of which drive it with
  real live-test manifests, including a missing manifest, a missing evidence
  directory, wrong scenario verdicts and mismatched artefact digests.

- **The verifier could confirm a `PASS` that the plan gate exists to refuse**
  (`dev`). `scripts/verify_carryover.py` re-derives which tasks deserve a `PASS`
  by running their tests — 400 of the plan's claims went through it — and it was
  the last script in `scripts/` that no test ran. Running it turned up two
  defects.

  `check_master_plan.py` refuses a `local` task marked `PASS` in as many words:
  nothing in this environment can produce its evidence. `evaluate()` did not know
  that rule, so a live task whose named regression test happens to pass on Linux
  and whose evidence path happens to exist came back `PASS` — one script writing
  precisely what the other exists to refuse. Demonstrated on the real pair before
  the fix: `evaluate` answered `PASS`, `check_master_plan.problems` answered
  *"E14-M01-T001 is a local task marked PASS; nothing in this environment can
  produce its evidence"* about the same task. It now returns no status at all for
  a `local` task, and `--apply` writes nothing — not the status, not the reason,
  not the commit, because recording a reason against a task this environment may
  not judge is still having had an opinion about it. The report prints those
  separately from the rejected ones: "cannot be judged here" and "was judged and
  failed" are different facts.

  Not reachable in today's plan — measured over all 84 `local` tasks, none has
  both an existing test file and an existing evidence path, and a test now holds
  that measurement. Closed anyway: "not reachable today" is a fact about the
  plan, which is edited every iteration, not about this code.

  Second defect, found by looking rather than by waiting for another red build:
  it invoked `.venv/bin/pytest`, the POSIX venv layout, which does not exist on
  Windows where the entry point is `.venv/Scripts/pytest.exe` — the same class as
  the decoding failure two commits ago. It runs `sys.executable -m pytest` now,
  correct on either platform and needing no venv at all.

  Checked and found sound, so it is on the record rather than implied: the
  refusal of a green run over zero executed tests genuinely works. pytest exits 0
  when every test in a target skips, and `tests/unit/test_carryover_verification.py`
  (10 tests) asserts that against a target built to skip everything, with a
  control that a target which really runs is still accepted. The load-bearing one
  is neither: it asks both scripts about the same task and requires that anything
  the verifier would confirm, the gate would accept — the invariant that was
  violated, and the one that catches this without anybody thinking of `owner`.
  Proved by planting the pre-fix script: four of the ten fail.

- **The decoding control test asserted the wrong failure, and the right one is
  worse** (`dev`). The production fix of the previous entry worked — the Windows
  runner went from `10 failed` to `1 failed, 8585 passed` at `6794dd4` — and the
  one remaining red was the control test written to keep that file honest. It
  asserted that an unpinned `text=True` exits non-zero, which is true on POSIX
  and false on Windows:

  * POSIX decodes in `_communicate`, on the calling thread, so the
    `UnicodeDecodeError` propagates and the process dies.
  * Windows decodes in `_readerthread`. The exception is raised *there*, printed
    by the threading excepthook, and never reaches the caller —
    `subprocess.run` returns `returncode` 0 with **empty output**. The release
    log's traceback names that exact frame.

  So the original defect was not only "a gate that could not execute". Outside
  pytest — which is how `scripts/check.sh` runs it — `audit_pass._path_at` would
  have returned `""` for every file, `_defines("", node)` answers False, and the
  audit would have reported tasks as unproven whose tests are plainly there.
  Measured, not reasoned: patching `_git` to drop `git show`'s stdout exactly as
  the reader thread leaves it makes the audit report **82 invalid claims out of
  400**, every one fabricated, with no error anywhere — a gate whose whole
  purpose is not to make false accusations, making 82 of them silently. That
  shape now has its own regression test.

  The control now asserts what both platforms share: the decode raises *and* the
  content does not arrive. Verified in both directions — the pinned form
  delivers all 214520 characters of `CHANGELOG.md` under the same ASCII locale,
  which is precisely what the control refuses to see from the unpinned one.

  Also checked and found clean, so it is on the record rather than left open:
  the three `subprocess` call sites in the shipped `packages/` are all
  byte-mode. The defect was confined to `scripts/`.

- **A control-plane gate could not run on Windows at all, and took the release
  build red** (`dev`). `subprocess.run(..., text=True)` decodes the pipe with
  `locale.getencoding()` — UTF-8 in this container, **cp1252 on the Windows
  runner**. Every file here is UTF-8 and much of it is Russian, so
  `scripts/audit_pass.py`, whose `_path_at` reads whole files out of git
  history, raised inside subprocess's *reader thread*:

  ```
  UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 in position 3672
  ```

  a hundred times over. `10 failed, 8565 passed, 68 skipped, 1 error` on the
  `windows package` workflow at `b350572` — not a check reporting a problem, a
  gate that could not execute. The audit had been wired into `check.sh` one
  commit earlier and had been exercised only on Linux; this is what the second
  platform was for.

  The defect is not Windows-specific and finding it needed no Windows:
  `locale.getencoding()` answers ASCII under `LC_ALL=C`, and the same decode
  raises here. All nine text-mode subprocess call sites in `scripts/` now go
  through `scripts/_process.py`, which pins `encoding="utf-8"` and
  `errors="replace"` — replace rather than strict because every reader searches
  the text for an ASCII marker, and a gate that dies on one byte of one file is
  a gate that gets switched off.

  `tests/unit/test_script_output_decoding.py` (11 tests) runs the real scripts
  as real processes under an ASCII locale, including a control asserting that
  the *unpinned* form still fails there — without which a future Python making
  `text=True` mean UTF-8 would leave the whole file proving nothing. Measured
  and recorded rather than assumed: with the fix reverted only `audit_pass.py`
  fails behaviourally, because it is the one whose child output carries prose;
  `check_master_plan.py` and `reconcile_status.py` shell out for SHAs and path
  lists that are ASCII *today*, so their bug is latent and a non-ASCII filename
  would wake it. That is why an AST check over every call site is here beside
  the behavioural ones, itself proved in both directions against a planted call.

  `tests/unit/test_master_plan.py`'s scratch repository now carries
  `_process.py`: it copies a fixed list of scripts, and without the module they
  import the subprocesses failed at import rather than at the thing under test.

- **Seven `PASS` claims named a proof that did not exist yet, and the gate that
  would have said so had never been run** (`dev`). `scripts/audit_pass.py` asks
  the questions `check_master_plan.py` cannot: that gate reads the tree as it
  stands today, so a task can name a commit that predated the proof it claims
  and every check passes. The audit asks the tree as it *stood* — and nothing
  invoked it. It is in no workflow, no `check.sh` step and no test. Running it
  took 2.1 seconds and returned eight invalid claims out of 400.

  Seven were real. The E11 packaging tasks name
  `tests/unit/test_windows_workflow_contract.py` as their regression test, and
  that file was first added at `f4fa0b2` — a *descendant* of every commit those
  tasks recorded as their verification, so at the commit each named as its proof
  the file was simply absent. The behaviour was there and the test passes today;
  what was false was the claim about where it had been proved.
  `verification_commit` now points at `f4fa0b2`, whose eight tests match the
  seven `pass_criterion` lines one for one (`console=True` →
  `test_the_spec_builds_a_console_executable`, `a red gate stops the upload
  step` → `test_a_red_gate_stands_between_the_suite_and_the_upload`, and so on).
  No `PASS` was withdrawn, because a real proof backs each; no question was
  relaxed.

  The eighth was a false accusation by the audit itself. `E06-M04-T001`'s proof
  sits exactly at the commit the plan names for it, and the audit looked at
  `commit` — the implementation — because it never read `verification_commit` at
  all. The two fields exist because those are different events, and
  `check_master_plan.py` says as much: a proof written after the code it proves
  is ordinary work. An audit that accuses a sound claim gets argued with once and
  then switched off, so this counted as a defect and was fixed with the rest;
  `audit_pass.proving_commit()` reads the verification commit and falls back to
  `commit` only when a task records nothing else. The docstring also claimed a
  fourth question — "does the regression test pass today?" — that no line of the
  file asked; it now says where that question is actually answered.

  `scripts/audit_pass.py --quiet` is a step of `scripts/check.sh`, and
  `tests/unit/test_pass_audit.py` (12 tests) runs the audit against the real plan
  and plants each of its questions: a proof dated before it existed, a named test
  node missing from a file that was there, an evidence path not on disk, a `PASS`
  standing on a reopened dependency, a task with no commit at all. Two more pin
  the false-accusation half — the task whose proof legitimately post-dates its
  implementation must stay clean, and a task naming only one commit must still be
  asked the question. Proved by planting each defect back in the real tree:
  restoring the seven commits fails two tests, restoring the pre-fix audit fails
  five, and dropping the `check.sh` step fails one.

- **The progress counter reported zeroes for a tree at 73.31%** (`dev`).
  `scripts/progress_report.py` and `scripts/check_progress.py` count and gate
  `docs/control/PLAN.md`, the 100-step plan. The plan of record moved to
  `docs/control/MASTER_PLAN.yaml` and `docs/control/STATUS.json` was regenerated
  in the new shape, with no `steps` key at all — and the counter reads every
  field through a `.get` with a default, so it went on printing a complete,
  confident report made entirely of those defaults:

  ```
  PROGRESS: 0%          STEP: 1/100        STATUS: NOT_STARTED
  RC ARTIFACT: None     LIVE SCENARIOS: 0/20
  EVIDENCE: 0 path(s) recorded in docs/control/EVIDENCE_INDEX.md
  ```

  against a file recording 73.31%, 400 of 484 tasks PASS, a release candidate
  identified by commit, run and sha256, and a catalogue of 22 scenarios. The
  `--write` form — the one `docs/control/COMMAND_LOG.md` told an operator to run
  to "recount and store" — then stored `overall_percent: 0` and six more zeroed
  keys into the file whose own `$comment` says every field is derived and a
  hand-written value is the defect it exists to prevent.

  Both scripts now ask `STATUS.json` which plan it describes and refuse when the
  answer is not theirs, naming that plan and its successor
  (`master_report.py` / `check_master_plan.py`); `progress_report.py` exits 1,
  `check_progress.py` exits 2 — the code its docstring had always documented for
  an unreadable file and which `raise SystemExit("...")` could never produce.
  The literal `20` in the live-scenario denominator is counted from the tally
  instead, the same drift already removed from `reconcile_status.py`.

  `tests/unit/test_control_plane_reporters.py` (9 tests) runs all four scripts as
  subprocesses: the retired pair refuses and names its successor, refuses before
  `--write` can touch the file, and still counts and gates a 100-step
  `STATUS.json` — the accepting direction, without which an unconditional refusal
  would pass. The fourth question is the one whose absence let this stand:
  `master_report.py` is run and its printed figures held against what
  `STATUS.json` records, both directions, percent for percent. Proved by planting
  each defect back in the real tree: the pre-fix counter fails four of the nine,
  the pre-fix gate two, and a `weighted_progress_percent` moved to 88.0 fails the
  agreement test.

### Added

- **§9 of the report called itself complete and was not** (`dev`). The section
  the standing instruction singles out — the exact list of steps that physically
  require the user to launch the game — had fifteen steps written from what this
  branch had been working on. The plan of record carries **84 tasks owned
  `local`** across six milestones, and comparing the two turned up five subjects
  with no step at all:

  - record the machine, its Windows version, and the capability scan, including
    that no capability reads `verified` without a live acknowledgement (`E14-M01`);
  - record the game incompatibilities the run finds, fix each, re-run every
    scenario a fix touched (`E14-M04`);
  - confirm no save file was corrupted — after the scenarios *and* after the
    endurance runs, which are different checks (`E14-M04`, `E15-M01`);
  - the endurance runs beyond `S19`/`S20` themselves: memory and handle counts
    stable, journals rotating without losing an observation, the character's
    outcome explainable from the trace (`E15-M01`);
  - a spoken stop halting the character, and the whole run recorded as a support
    bundle verified clean (`E14-M04`).

  Added as steps 14–18 rather than folded into the existing ones, so the omission
  stays visible; the release steps renumber to 19–20 and the one cross-reference
  moves with them. The panic stop is deliberately *not* a new step — `S18_PANIC`
  is one of the twenty-two and confirms it from the keyboard.

  `TestTheReportListsEveryMilestoneOnlyAGameCanClose` holds it: every milestone
  carrying `local` tasks must be named in §9. Steps 7, 9 and 20 gained their
  milestone ids so the mapping is visible to a reader rather than implied.

  Its limit is written into the docstring rather than left to be discovered.
  Deleting one step of a well-covered milestone leaves the id present and the
  check green — tried, on step 18, and it passed as designed. It fires when a
  milestone loses its *last* step, tried on 14. It holds the subjects, not the
  steps.

- **Nothing said where the release archive comes from** (`dev`). Every document
  describes what to do *with* `pz-agent-windows-*.zip` — `INSTALL.md` opens with
  a table telling the reader which installer their situation calls for — and
  none said how to obtain it. It is a workflow artifact, not a release asset.

  Worse, GitHub wraps it, and the wrapping is the trap I have fallen into
  roughly ten times while maintaining this repository: the download is
  `pz-agent-windows-rc.zip`, the release candidate is the
  `pz-agent-windows-v1.0.0-rc1.zip` *inside* it, and **the SHA-256 the Actions
  page shows beside the artifact is the wrapper's, not the archive's.** Those
  two numbers are never equal. A user comparing the page's digest against the
  one in `EVIDENCE_INDEX.md` would conclude they had the wrong file.

  `docs/LOCAL_GAME_HANDOFF.md` §5 now opens with getting the ZIP and checking
  it: extract, then `certutil -hashfile … SHA256` against the
  `.zip.sha256` sidecar that travelled with it, and against the `archive sha256`
  row in the evidence index. Three independent statements of one number — the
  builder wrote the sidecar, the gate printed it into the workflow log, the
  index records it against its commit and run — with the instruction to stop if
  any two disagree.

  No new test: the procedure rests on behaviour already pinned.
  `test_packaging_rc.py` fixes the sidecar's contents as
  `{report.sha256}  {name}`, and `test_rc_archive.py` fixes `report.sha256` as
  the digest of the archive's own bytes.

  The first draft linked the evidence index relatively and
  `test_archive_documents_resolve.py` refused it — `docs/control/` is not
  shipped, so that link would dangle for someone reading the document inside the
  ZIP. It is named rather than linked, and the reason is written beside it.

- **A gap `docs/PROGRESS.md` still listed as open had been closed** (`dev`).
  Its "Requires a live game session" section said of the restore guard:
  *"Nothing yet supplies it from an actual process check; whoever wires the CLI
  must, and a wrong answer here is the one that corrupts a save."* Not true, and
  not true for some time: `supervisor.probe_game_running` asks the game heartbeat
  first and falls back to the process table, `game_running_for_restore` collapses
  its three-valued verdict toward refusing, and `saves.py` passes that boolean to
  `BackupManager.restore`. Five tests in `test_cli_saves.py` hold the path,
  including the refusal when a process names the game and no heartbeat exists.

  Overstating what is left is the mirror of understating it, and this is the
  document the next reader starts from.

  The entry is narrowed to what a live install is genuinely still owed: the
  fallback matches the literal `GAME_PROCESS_MARKER`, `"zomboid"`, case-folded,
  and nobody has read the process table of a machine with Build 42.20 open. The
  direction of a wrong marker is the safe one — unreadable, truncated and
  non-matching listings all yield `MAY_BE_RUNNING` and refuse the restore — so it
  costs a refusal to work around, never a lost save.

  `docs/LOCAL_GAME_HANDOFF.md` §13 now asks for it, because the answer takes
  five seconds at the machine that has the game: `tasklist | findstr /i zomboid`.
  Stated as a question rather than a blocker, with why it is safe either way.

  `docs/SAFETY.md` and the handoff's own §14 table were checked and are accurate;
  only `PROGRESS.md` carried the stale claim.

- **The last mile of the operator's work, checked across the seam** (`dev`).
  The operator runs the scenarios inside Project Zomboid — the catalogue's
  declared budget is 20 460 seconds, five hours and forty-one minutes — then
  `pz-agent live-test finalize` writes `release/evidence-manifest.json` and
  `check_release.py --release` reads it. Nothing had ever put one side's output
  into the other's reader: `test_livetest_runner` asserts what `finalize`
  writes, `test_check_release` builds manifests by hand and asserts what the
  gate makes of them. Each side tested against its own idea of the document.

  The cost of that gap is not a red build. It is discovered *after* the hours in
  the game, and the evidence has to be produced again.

  `tests/contract/test_evidence_manifest_round_trip.py` drives a real evidence
  tree, calls the real `finalize`, and feeds the manifest to the gate's own
  `_scenario_verdicts` and `_artefact_digests`. It also tampers with a recorded
  artefact and requires the gate to catch it, so the pair is shown working
  rather than merely agreeing.

  Demonstrated on the realistic direction — the gate tightening, which no runner
  test can see. Changing the gate's comparison from `"PASS"` to `"PASSED"` left
  the runner's 116 tests green while every scenario of a completed run would
  have been rejected; the round trip fails.

  Scope is stated in the file rather than implied: two scenarios are driven, not
  twenty-two. The fixture supplies observations shaped per scenario, and
  inventing them for the other twenty to satisfy a postcondition is what this
  repository refuses on the critical path. So the file checks the *document*
  across the seam and passes the scenario list explicitly; completeness stays
  `finalize`'s own guard, which `test_livetest_runner` already proves refuses a
  tree that is missing, unpassed or tampered with.

- **"Documented" meant a transcription of the document, not the document**
  (`dev`). `tests/unit/test_mcp_catalog.py` carries `DOCUMENTED_TOOLS` and
  `DOCUMENTED_RESOURCES` — literals whose comment reads *"the set named by
  docs/MCP_TOOLS.md, written out rather than derived, so a tool appearing or
  vanishing has to be a deliberate edit in two places"*. Sound intent, wrong two
  places: they were `catalog.py` and the test file. The page itself was never
  read by anything.

  Renaming `pz_action_inspect_recipe` out of `docs/MCP_TOOLS.md` left **134 tests
  green** while the page an MCP client author works from no longer named a
  published tool — and that one is the reading the catalogue's own comment says
  a client is expected to call first.

  The 49 tools and 7 resources do agree with the page today; nothing was wrong,
  and nothing was holding it. `test_the_document_names_every_published_tool_and_resource`
  now reads the file and compares both directions: published-and-undocumented is
  a client that cannot find a tool, documented-and-unpublished is a client that
  calls something the server refuses. The hand-written literals stay as the
  deliberate-edit tripwire they were meant to be.

  Verified by planting each direction against the real page — a dropped tool, an
  invented resource URI — and removing them again.

- **Two counts of the wrappers, and nothing comparing the collections**
  (`dev`). `build_rc.BAT_NAMES` decides which `.bat` files the archive carries.
  `tests/unit/test_packaging_rc.py` asserted `len(BAT_NAMES) == 11`;
  `tests/contract/test_bat_wrappers_invoke_the_real_cli.py` asserted
  `len(glob("*.bat")) == 11` over the directory. Two literals, no set
  comparison — so a wrapper added to the directory and not to `BAT_NAMES` is
  never copied into the archive, and the only thing that reddens is a count.

  The natural repair for a red count is to bump it. Doing exactly that was
  tried, on the real tree: an undeclared `latency.bat` in the directory, the
  directory count raised to twelve, `BAT_NAMES` untouched. **188 packaging tests
  went green while the archive shipped eleven wrappers and the twelfth existed
  only in the repository** — a file a document could tell a user to
  double-click, that is in no release.

  Both literals are gone. The wrapper contract now compares the two sets in both
  directions and derives the count from them, and the message names the file. An
  undeclared wrapper fails with its own name; a declared wrapper with no file
  fails the same way. Verified by planting each direction separately and by
  removing them again.

  Reported alongside what was *not* found: the reverse omission — a `.bat`
  present and undeclared — was already caught at collection time by the same
  contract's directory count, so this is a hole in how the two checks were
  written rather than a hole in coverage nobody had thought about.

- **The mod's identity is spelled in six places and was checked in none**
  (`dev`). `modinstall.py` carries `MOD_ID: Final = "pz_agent_bridge"` under a
  comment reading *"Directory name under `Zomboid/mods`. Matches `id=` in
  `mod.info`"*. The comment stated a relationship; nothing tested it. Both
  `mod.info` files declare the id independently, and `installer/INSTALL.md` and
  `docs/QUICKSTART.md` print the path a user checks by hand.

  They agree. Nothing held them together, and the outage they would cause is one
  this project has already met twice: `test_mod_info_declares_the_same_mod_version`
  records that on 2026-08-08, against Build 42.20.2, **the mod simply did not
  appear in the mod list** — once for `pzversion`, once for an empty `require=`
  line. That test pins both of those in both files. It does not pin `id`.

  A divergence is quiet in the worst way: the installer writes
  `Zomboid/mods/<MOD_ID>/mod.info` whose `id=` says something else, the verifier
  and the documents follow the constant, the game reads the file, and the user is
  told the mod is installed and does not find it.

  `tests/contract/test_mod_identity_agreement.py` holds the five declarations
  together. Proved by planting each divergence separately against the real tree —
  a renamed id in `pz-mod/42/mod.info`, a renamed `MOD_ID`, a stale path in
  `INSTALL.md`. Two of the three are caught *only* here: the versioned
  `mod.info` and the install document both pass `test_lua_mod_contract.py` and
  `test_cli_modinstall.py` unchanged.

  Test fixtures that write `id=pz_agent_bridge` are deliberately excluded — they
  are inputs to tests, not declarations of the product.

- **The ack seam, checked in the one direction nothing could see** (`dev`).
  Written expecting to find the ack unchecked, the way the observation document
  was. It is not: `Handle:ack` is pinned hard by `tests/lua/test_action_runtime.lua`,
  and three plants against the real mod — dropping `schema_version` from the
  record, mapping `LOST` to a non-terminal status, removing `INTERRUPTED` from
  `TERMINAL_PHASES` — each failed that suite already. Reported as found rather
  than dressed up as a discovery.

  What remained is one direction, and it is the realistic one: **the sidecar's
  reader can tighten, and no Lua test can know that it did.**
  `ActionResult.from_dict` requires eight fields, refuses an unknown reason code
  outright and wants both ids UUID-shaped; the mod's suite checks the fields it
  knows to check. Adding a requirement to `from_dict` that the mod does not
  satisfy leaves every Lua suite green — verified by planting exactly that — and
  fails `tests/contract/test_action_ack_round_trip.py`.

  `tests/lua/support/dump_action_acks.lua` drives commands through the real
  `ActionRuntime` with spy adapters and prints the acks the journal actually
  received; the contract test reads them with the sidecar's own reader. It also
  holds `finished_at_ms` against `ActionStatus.is_terminal`, which is a
  correspondence across a rename — `interrupted` is a mod phase that travels as
  `cancelled` — so the two tables cannot agree by matching names alone.

  Worth recording: a spy that reports `done` still ends `failed /
  POSTCONDITION_FAILED` in this fixture, because the runtime asks for an
  observed postcondition and there is no game to observe one in. The dumper's
  scenarios are therefore keyed by what the adapter was told to do, never by the
  outcome expected of it.

- **The protocol vocabularies the regex could not see** (`dev`).
  `tests/unit/test_lua_mod_contract.py` holds `Protocol.lua` against the Python
  enums by matching `KEY = "value"` pairs in the file's text. Four tables are not
  written that way — `ACTIONS`, `TERMINAL_STATUSES`, `MUTATING_MODES` and
  `DANGER_RANK` are built by `toSet(...)` and by keying off other tables — so the
  pattern found nothing in any of them and they were left unchecked. Three carry
  decisions that have to hold on both sides of the wire: when the mod stops
  tracking an action, which session modes accept a world-changing command, and
  the order the reflex guard compares danger against.

  They agreed already; nothing was broken. What was missing was anything keeping
  them that way. `tests/lua/support/dump_protocol_tables.lua` now loads the module
  the way the game loads it and prints the tables it ends up holding, and
  `tests/contract/test_protocol_tables_agreement.py` compares fourteen of them to
  the Python side. This is the fourth seam checked by running the producer rather
  than by reading the source, after adapter args, adapter capabilities and the
  observation document — and it is the retraction's lesson applied on purpose: a
  producer written through a constant was invisible to a regex once already.

  Demonstrated both directions on the real mod, not on a synthetic. Dropping
  `Protocol.STATUS.LOST` from `TERMINAL_STATUSES` — an action the sidecar retires
  and the mod would nurse forever, which is the NEVER TERMINAL family by name —
  leaves the entire existing Lua contract suite green and fails the new check.
  Ranking `HIGH` equal to `MEDIUM` in `DANGER_RANK` does the same. Both were
  reverted; the tree is unchanged.

  These fourteen need a Lua interpreter and so skip on the Windows release
  runner, like the two seam checks before them; CI's Linux leg runs them.

- **The final report re-measured at the current head** (`dev`). Every figure in
  it was produced by running something, and the plan's regeneration moved most
  of them: 480 tasks → 484 and 3104 weight → 3144, so 74.3% → **73.3%**; LIVE
  GAME VALIDATION 599 → 639; the suite 8508 collected → **8586**, 8503 passed
  and 5 skipped here, 8537 and 49 on CI's Windows leg. The RC identity is now
  `f397d21` / run 31814473578 / `8be6712c…`, and the local executable-less
  archive block was rebuilt rather than edited — `fdf27768…`, 75 entries, the
  same two `[FAIL]` lines, which is the gate refusing a build without its
  executables and therefore working.

  §7 and the header now name `c4320f0` over code tree `f397d21`. The percentage
  went down, which is what a corrected denominator does; nothing regressed.

- **The report's operator section is no longer exempt from the command checker**
  (`dev`). `FINAL_IMPLEMENTATION_REPORT.md` is in `RECORDS` — the three files
  allowed to quote a broken command, because a report describing `pz-agent logs
  --redact` has to be able to print it. But the exemption is file-wide, and §9
  is not a record: it is the operator's step list, read with the game running,
  where a command that fails costs the most.

  The exemption is now narrowed by structure rather than by trust. §9 is located
  by its heading — so it survives a rewrite — and every invocation in it goes
  through the parser; the quoting sections stay exempt. Verified by planting
  `pz-agent capabilities --verify` in §9 and watching the check refuse it, then
  removing it and watching the check pass.

- **Twenty-nine plan tasks told the operator to run commands that do not exist**
  (`dev`). `tests/contract/test_documented_commands_parse.py` has put every
  `pz-agent …` line in every shipped document through the real parser since two
  pages were caught naming `--redact` and `memory --forget`. It reads `*.md`.
  `MASTER_PLAN.yaml` is YAML, and its `verify_command` fields are command lines
  a person types — so nothing had ever looked at them.

  Through that gap: all twenty-two of `E14-M02` verify with `pz-agent
  capabilities --verify`, and there is no `capabilities` command; `E14-M01-T002`
  says `pz-agent backup`, which is `backup-save`; four `E15` tasks say
  `pz-agent logs --trace`, and there is no `--trace` flag. Together with the
  `livetest` spelling fixed in the previous commit, that is twenty-nine tasks —
  all of them in the two epics nothing here can close, so the first person to
  try one would have been at a Windows machine with the game running, working
  through a checklist that does not run.

  The capability scan is `doctor` ("check the installation and the capability
  surface"), the resolved ledger is what `status` reports, and the trace is read
  back by `replay`. The plan now says so.

  The checker gained a section for the plan: sixty invocations, each parsed, and
  a guard that fails if `E14-M02` or `E14-M03` stops being covered. Verified
  against the plan as it stood before this commit — twenty-nine rejections, the
  exact four shapes — and against the plan after it, zero.

- **The plan of record told the operator to run scenarios that do not exist**
  (`dev`). `E14-M03` — the milestone a person at a Windows machine works through,
  on one of the two epics that gate `v1.0.0` — was generated from a hand-written
  list of twenty `(code, name)` pairs in `scripts/plan_epics_d.py`, and the
  runner's catalogue had moved out from under it. The plan's `S05` was "open a
  container"; `S05_BLOCKED_PATH` is a walk into a wall. Eighteen of the twenty
  named a scenario other than the one their id selects, `S21_CRAFT` and
  `S22_BUILD` had no task at all, and every row's verify command read
  `pz-agent livetest run S05` — a subcommand the CLI does not have — against
  `evidence/live/S05/result.json`, a directory `EvidenceLayout` has never
  written to.

  The list now comes from `pz_agent_cli.livetest.scenarios.SCENARIOS`, so id,
  title and evidence path all come from the object the runner runs. The commands
  are `pz-agent live-test run --scenario <ID>` and the paths
  `evidence/<ID>/result.json`. The other `livetest` invocations in E14 are
  corrected too, including three calls to a `report` subcommand that does not
  exist, and the manifest rows now name `release/evidence-manifest.json` rather
  than `evidence/live/manifest.json`. The plan grows from 480 to 484 tasks and
  from 3104 to 3144 weight — the two scenarios that had no task, in both the API
  and the run milestones.

  Four tests in `tests/unit/test_master_plan.py` pin the correspondence in both
  directions: every catalogue scenario has a task, no task names a scenario the
  catalogue lacks, each evidence path is the one the runner writes, and each
  verify command's subcommand is asked of `livetest.commands.SUBCOMMANDS` rather
  than compared against a list written beside it.

- **A hardcoded twenty outlived the twenty scenarios, again** (`dev`).
  `scripts/reconcile_status.py` carried `"not_run": 20` as a literal, so
  `STATUS.json` told every reader that twenty live scenarios were waiting on a
  game while the runner owed twenty-two. This is the same defect the CLI had at
  `live-test status`, fixed there by replacing the word — and a spelled-out
  constant is fixed where somebody happens to look, so this file kept its copy.

  The generator now counts `SCENARIO_IDS`, and an unreadable catalogue is a hard
  failure rather than a fallback: a fallback would be a guess, and a guessed
  number in `STATUS.json` is the class of value that script exists to prevent.
  Three tests hold it — the generator counts (proved by running it in a scratch
  repo), the committed artefact agrees with the catalogue, and the dictionary
  STATUS is built from carries no scenario count of its own.

  The same wrong number is corrected in `docs/LOCAL_GAME_HANDOFF.md` (four
  places, including *"S01–S20 live scenarios"*), `scripts/check_release.py`,
  `packaging/windows/README.md`, `app.py`, `livetest/commands.py`,
  `livetest/runner.py` and `tests/contract/test_sidecar_writes_its_log.py`,
  where "nineteen of the twenty scenarios name `pz-agent.log`" is now the
  twenty-one of twenty-two that do.

- **The handoff described a release candidate built without its executables**
  (`dev`). `LOCAL_GAME_HANDOFF.md` §1 named `dist/pz-agent-windows-v1.0.0-rc1.zip`
  — the local `build_rc.py` output, useful for checking the layout and missing
  both Windows executables, because they are compiled on the Windows runner. The
  row now names the `windows package` artefact as the one of record and points
  at `EVIDENCE_INDEX.md` for its commit, run and digest.

- **The retraction's last live carrier was the page a user reads when a move
  fails** (`dev`). `TROUBLESHOOTING.md` had an entry titled *"TARGET_NOT_LOADED"
  on every move — known, unfixed*, which told the reader the mod emitted no
  square tier at all and that "the character cannot be walked anywhere by the
  agent until one of them changes". Wrong since the crafting wave, and the worst
  place to be wrong: this is the page consulted at the moment something breaks.

  Rewritten. A single `TARGET_NOT_LOADED` now means what it says — the square
  was not described in this observation, so move closer. *Every* move refusing
  is redirected to the likely upstream cause: `Observe.describeSquares` calls
  `isSolid`, `isSolidTrans`, `isFree` and `getFloor`, none of which is confirmed
  against Build 42.20, and a build exposing none of them leaves every square
  without a passability reading — with what to check and what to report. The
  genuinely unfixed part, the storey-changing move, is stated as the narrow
  thing it is.

  A sweep of every document under `docs/` now finds no live carrier of the
  retracted claim. `CHANGELOG` keeps its own history, which was true when
  written and is marked as retracted where it was not.

- **The handoff still carried the retracted claims, and it is the one document a
  live session is planned from** (`dev`). `LOCAL_GAME_HANDOFF.md` §4 opened with
  "eight parts of the sidecar are wired to a mod that cannot drive them. Two of
  them mean the agent cannot walk and cannot loot", and its first row told the
  operator to *skip every scenario that walks*. The walking claim was retracted
  several commits ago; the document was not. That is the same stale-document
  defect this pass exists to remove, aimed at the person who would have spent a
  session obeying it.

  The row is replaced by the two narrow gaps that survived — a floor-changing
  move refuses, a closed-window square is refused under the wrong name — and the
  container row no longer says "blocked twice over", because the mission can now
  reach the crate and is refused at it. The priority list gains walking as
  something newly worth doing, and the symbol row now names `isSolid`,
  `isSolidTrans`, `isFree` and `getFloor` as the four the square tier's
  correctness actually rests on.

  The section also now opens with the command that shows all of it without the
  game, and says why that matters here: this table has been wrong before, and
  the test runs the mod instead of reading it.

- **The last live gap is now a refusal something can be pointed at** (`dev`).
  The dumper stands a crate on a nearby square. The mod's `buildObject` mints it
  a proper `container:` reference — a planner sees it and can name it in a goal —
  and `InventoryView.container` returns `None` for that reference, because
  `inventory.containers` holds only the character's own roots and nothing ever
  adds a world container to them.

  Both halves are asserted, because the gap is the gap *between* them: nameable
  and unresolvable. That is why `loot_area` cannot take anything out of
  anything, and it is the only one of this build's three known gaps that still
  costs a whole goal kind. Until now it was a row in a ledger and a paragraph in
  `LIMITATIONS.md`; it is now a document a reader can run.

  With this the round trip covers all four tiers of the observation document —
  items, squares, zombies, containers — and each was verified by breaking the
  mod rather than by reasoning about it.

- **The round trip reaches the zombie tier, which is the one where being wrong
  is dangerous** (`dev`). The document now carries two zombies built by the mod:
  one with a readable target, one whose target reader the build does not expose.
  The first arrives `chasing=True`; the second arrives with the key **absent**,
  which is what keeps `NearbyZombie.chasing`'s `None` reachable at all.

  That distinction is the mod's own stated rule — "we could not tell" must not
  look like "it is not chasing" — and it had never been checked across the seam,
  only asserted on each side separately. A `False` arriving where nothing was
  read understates the threat, and the reflex guard is downstream of it.

  Sensitivity verified through the producer again: making `buildZombie` decide
  an unread chase as `false` fails the test on exactly that assertion. Three
  tiers of the observation document are now round-tripped — items, squares and
  zombies — and each was checked by breaking the mod rather than by reasoning
  about it.

- **The round trip now covers the tier where the retraction happened** (`dev`).
  The first version walked the inventory only. The `nearby` tier is where a row
  claimed for four commits that the mod emitted no `kind = "square"` entry — the
  claim that cost `LIMITATIONS.md`, `PROGRESS.md` and two sections of the final
  report before it was caught. The dumper now installs a real square window,
  `Observe.nearbyFields` walks it, and the document carries 49 square entries
  with `loaded`, `blocked` and `occupied` semantics.

  Three assertions follow from that, each pinning something that was previously
  only argued: the mod really does publish squares, and every entry carries the
  position movement matches on; `closed_window` and `stairs` really are absent
  from the square entry, which is the narrow gap that survived the retraction;
  and `policy.building.read_window` — the consumer a retracted row called dead —
  builds a window from the mod's own document rather than returning `None`.

  Sensitivity verified through the producer, as before: making `mergeNearby`
  return early makes the first of the three fail with the message naming which
  documents to un-retract. The check that would have caught the mistake now
  exists, and it exists as a test rather than as a paragraph.

- **The observation seam now has the check the command seam has had all along**
  (`dev`). `tests/lua/support/dump_observation.lua` builds one document through
  `Observe.playerFields`, `Observe.inventoryRoots` and `ObserveModel.build` —
  the shipped code — against fakes that stand in for the *engine* and answer
  exactly the accessor names the mod's own readers ask for.
  `tests/contract/test_observation_document_round_trip.py` decodes it with
  `Observation.from_dict` and hands the items to the typed views the policies
  use. One document, both real implementations, one process.

  This is the twin of `test_adapter_args_agreement.py`, pointed the other way,
  and its absence is why every one of the eight dead gates was found by hand:
  the sidecar's fixtures build the document the sidecar expects, the mod's
  suites build the document the mod emits, and nothing put one side's output
  into the other side's reader.

  It catches what a key-set comparison cannot. The blocks are raw `JsonDict` on
  both sides, so a disagreement is not a type error or a crash — it is a
  decision coming out wrong. The mod reports a sandwich `rotten: true`,
  `burnt: true`, `poisonous: true`; `FoodView.is_rotten` answers **False**
  because it asks whether `freshness == "rotten"`. That is the whole defect
  class, demonstrated end to end instead of argued from two files, with
  `poisonous` asserted beside it because that key does cross and must keep
  crossing. The literature block is pinned the same way, from the other side:
  `pages_total`, `min_level` and `max_level` now survive the trip, which is what
  the crafting wave's rename looks like from the sidecar's chair.

  Verified in the direction that matters, and through the producer rather than a
  synthetic: splicing a real `freshness` emission into `Observe.itemFood` makes
  the test fail with the message telling the next person which documents to
  update. A regex over the sources can be fooled by how a producer is written —
  one gate row was, for four commits. A test that runs the producer cannot be.

- **The crafting and building wave is merged into the line** (`dev`). Six
  commits, 19 097 insertions across 80 files, carrying `craft_item` and
  `build_structure` as the twelfth and thirteenth deterministic goal kinds, the
  policies behind them, and the mod-side readers they need. Four files
  conflicted and each was resolved by keeping both sides rather than picking
  one; the two that mattered are recorded under *Fixed* below, because in both
  the merge would otherwise have silently dropped a safety property this branch
  had added.

  Two producers arrive with it that close standing gaps. The literature block's
  three drifted key names are renamed on the mod's side (`pages` →
  `pages_total`, `skill_level_min`/`skill_level_max` → `min_level`/`max_level`)
  and `unread_recipes` gains a real reader, so that block's vocabularies now
  agree on all six keys the mod sends — the first of the three divergent blocks
  to be repaired, and the worked example for `food` and `fluid`. And squares are
  observed at last, as a `nearby.squares` tier with `loaded`/`passable`/`free`/
  `floor` read tri-state, published for the build policy's path check.

### Fixed

- **The retraction's own file still carried the retracted claim** (`dev`).
  `_mod_sources`' docstring in `test_gates_without_producers.py` went on saying
  that the wave's squares are a separate tier and that "`movement.py` still
  scans `nearby.objects` for `kind == "square"` and still finds nothing" — the
  sentence the retraction had just disproved, sitting in the file the retraction
  was about. The module docstring was corrected and this one was missed.
  Comment-stripping stays, for the reason it was added rather than the reason it
  was written down: prose about a gap must not be mistaken for the thing that
  closes it.

- **The vocabulary ledger's extractor now proves it is not reading by
  indentation alone** (`dev`). `_mod_keys` finds a reader's keys with
  `^\s{4}(\w+) =` — exactly four spaces, true of every reader today and not a
  property of Lua. A key nested one level deeper would vanish from the extracted
  set, and a vanished key does not fail loudly: it lands in `UNSENT`, where it
  reads as *the mod does not send this*. That is the same false negative that
  kept a retracted row alive for four commits, one file over.

  The count is now taken twice, strictly and at any indentation, and the two
  must agree. Verified in the direction that matters — against a synthetic body
  written *unlike* the pattern, where the control does fire — rather than
  against one written the same way the pattern is, which is precisely the check
  that fooled itself last time.

- **Every other dead-gate row re-verified after the retraction; all held**
  (`dev`). One row having been wrong for four commits is a reason to distrust
  the other seven, since all were checked the same flawed way — a pattern
  matched against a producer written the same way the pattern was. Each was
  re-derived by finding *every* assignment of the key in the mod and asking
  which function it belongs to: `accessible`, `full` and `present` are written
  `true` at every production site; `action_type` does exist in the mod, on
  `Ownership.panicPlan`'s per-entry records, and not on the `describe` table
  `ObserveModel.action` actually reads, so the §17.2 rung is still dead;
  `CONTAINER_KIND.WORLD` is minted inside `buildObject` as a reference for a
  nearby object, which is exactly what its row claims, and nothing adds the
  crate to `inventory.containers`; the mod builds no `extra` table at all, so
  the weapon row stands. The re-derivation and its method are recorded in the
  file's own docstring — the pattern is the alarm, not the argument.

  Also fixed: `EVIDENCE_INDEX.md`'s "certified by" row still read 8467 of 8508
  after the archive, digest, commit and run rows had been moved to the newer
  RC. It now reads 8468 of 8509 and says why the suite grew by one.

- **Retracted: the agent can walk, and `build_structure` is not dead** (`dev`).
  This branch asserted twice, in `LIMITATIONS.md`, `PROGRESS.md` and the final
  report's §9 and §10, that the mod emits no `kind = "square"` entry — that no
  navigation leg could succeed and that every placement refused
  `WOULD_TRAP_PLAYER`. Both claims were false. `ObserveModel.buildSquare` mints
  the entry with `kind = ObserveModel.SQUARE_KIND` (`SQUARE_KIND = "square"`), a
  square reference, a position and a semantics list, and `mergeNearby` folds the
  bounded square list into `objects` before `ObserveModel.nearby` returns. Both
  `movement._find_square` and `policy/building.read_window` find what they scan
  for. The producer arrived with the crafting and building wave.

  **How the check that produced the claim fooled itself matters more than the
  claim.** The ledger row searched the mod for a *literal* `kind = "square"`;
  the mod declares the token once as a constant and refers to it everywhere. The
  row's both-directions discipline — no match today, a match when a plausible
  producer is spliced in — was satisfied by splicing in a producer written the
  same literal way the pattern was, so it tested the pattern against itself. The
  one match it ever found was a *comment* in the new section explaining the gap,
  and stripping comments made it pass for the wrong reason.

  `test_gates_without_producers.py` now carries a second positive control: the
  square semantics the mod demonstrably sends must be findable, and the
  `SQUARE_KIND` declaration must be visible. A checker blind to the producer's
  spelling cannot report an absence, whatever its rows say.

  What survives is narrower and is about **where** a semantic lives. Three of
  movement's five square tokens cross — `loaded`, `blocked`, `drop` — plus
  `occupied` for the build policy. `closed_window` and `stairs` are read off the
  square entry and put by the mod on the *object* standing there, deliberately:
  "emitting them here would be the same fact in two places, free to disagree."
  So a floor-changing move always refuses `PATH_NOT_FOUND` (toward caution), and
  a square behind a closed window is refused as `blocked` rather than under its
  own name. One ledger row covers both; the world-container gap is now the only
  one of the three that still costs a whole goal kind.

- **The final report is re-measured at the merged head, and §9 names what will
  fail** (`dev`). `FINAL_IMPLEMENTATION_REPORT.md` was pinned to `3d8078d`, some
  150 commits back, and its §8 still named an artefact from a run two RCs ago.
  Every figure in it was re-taken rather than adjusted: 8508 tests collected
  across 229 files, 4821 Lua assertions across 32 suites summed from the
  harness's own per-suite lines, `ruff`/`mypy`/gate output quoted from a fresh
  `check.sh`, the local RC rebuilt and re-refused by `check_release.py --rc`
  (75 entries, `36f7357b…`), and the artefact of record replaced with CI's
  (`1540df78…`, 77 entries, run 31734696473). The playbook's time budget was
  re-summed from its own `**Time budget:**` lines — 20 460 s, 5 h 41 min across
  22 scenarios — rather than adjusted from the twenty-scenario figure.

  §9, the section `docs/RELEASE.md` calls "the one that keeps the rest honest",
  gained the three abilities now known not to work against the shipped mod:
  the agent cannot walk, `build_structure` refuses every placement, and nothing
  loots a world container — each with its mechanism, and with the note that two
  of the three are one missing producer, so one contract decision settles both.
  A live session should not spend its time diagnosing them, and §10 no longer
  lets "8467 tests passed" be read as "the agent plays the game".

- **A hardcoded "twenty" outlived the twenty scenarios** (`dev`).
  `pz-agent live-test status` printed "All twenty need a running game" directly
  underneath a tally reading `NOT_RUN 22`, and `resume` and the subcommand help
  said the same. The test covering that line asserted the literal string
  `"all twenty"`, so the one check that should have caught the drift was what
  held it in place. All four now count `SCENARIO_IDS`, and the test asserts
  agreement with that length instead of a spelled-out word.

- **The building half of P5 cannot succeed once, for the reason the agent
  cannot walk** (`dev`). Found by asking the merge the question the dead-gate
  ledger exists for: does the arriving sidecar logic decide on values the mod
  has no path to produce? The crafting vocabulary came back clean — every key
  `policy/crafting.py` reads has a producer. The building one did not.

  `policy/building.read_window` builds its enclosure window by scanning
  `nearby.objects` for `kind == "square"` — the identical scan `movement` has
  been failing on since it was written — and returns `None` when it collected
  none, which against the shipped mod is always. Its caller refuses
  `WOULD_TRAP_PLAYER` on `None`, so **every `build_structure` placement is
  refused**, blaming the map rather than the seam. The direction is the safe
  one and deliberately so — an unreadable map is where a trapping wall is most
  likely, not least — but the goal cannot succeed, exactly as `LEARN_RECIPE`
  could not before its producer arrived.

  The wave *does* publish squares, as a separate `nearby.squares` tier for its
  own path check. Neither consumer reads it. Not repaired here, because which
  side moves is the same cross-language contract decision the square tier has
  always been — but the case for settling it is now three-for-one: movement,
  the enclosure check and `loot_area`'s approach all wait on it. Recorded on
  the existing ledger row rather than a new one, since it is one missing
  producer with a second consumer.

- **The merge would have wedged craft and build missions, and mypy caught it**
  (`dev`). `_collect_mission_pending` returns a three-state `_Pending` on this
  branch — `RUNNING`, `SETTLED`, `LOST` — while the arriving craft and build
  handlers read it as a boolean, and every enum member is truthy, so both
  handlers returned `None` on every tick and neither mission could ever take a
  step. `--strict` reported it as two unreachable statements rather than as the
  dead goal kind it was. Both now branch on the three states exactly as the
  other six do, including ending the goal typed on `LOST`.

  Their submit paths carried the original defect too, with the same borrowed
  comment ("the mission decides again from the next observation"), which is
  false for a mission: `CraftItemMission` and `BuildStructureMission` both set
  `_pending_action` when they *emit* a step and decline every tick while it
  stands, so a swallowed `LoopError` left the goal to die on its wall clock
  reporting a timeout for what was a refused admission. Both now end the goal
  `CAPABILITY_UNAVAILABLE`/`UNADMITTED_STEP_DETAIL`, as the other six do. It
  bites hardest on build, the one irreversible thing the agent does, where a
  goal that runs out of clock says nothing about whether the placement was
  attempted.

- **The dead-gate ledger nearly retired the largest gap in the build on a
  comment** (`dev`). The arriving squares section opens by explaining that
  `movement` has been scanning `nearby.objects` for `kind = "square"` entries
  nobody publishes — and that sentence matched the very pattern standing guard
  over the gap, so the ledger reported the producer as written. It is not: the
  wave publishes a separate `nearby.squares` tier, and `movement.py` still scans
  `nearby.objects` and still finds nothing, so **the agent still cannot walk**.
  `_mod_sources` now strips Lua line comments before matching, and
  `LIMITATIONS.md` states the changed reason: the fix is no longer to write the
  producer but to point one side at the other, which is a contract decision a
  live game has to settle.

  `_mod_keys` had the mirror-image blind spot: it pinned readers to exactly
  `(item)`, so `itemLiterature(item, isKnown)` — a reader that grew a second
  argument in order to answer honestly — read as a reader that had vanished.
  Both extractors now survive the seam getting better, which is the failure mode
  that quietly retires ledger rows.

- **Nothing could ever be picked to learn a recipe from**
  (`stabilize/arm-session-confirmation`). `LiteratureView.unread_recipes` was
  read with `read_int`, whose job is to substitute a default, and no mod reader
  publishes the key on any build. Every magazine therefore reported zero unread
  recipes, and `_filter_recipes` refused each one for the reason "nothing new in
  it" — the `LEARN_RECIPE` goal could not be served by any item in the world,
  while the refusal named the magazine rather than the missing reader.

  The field is now tri-state, absent meaning **unknown** rather than zero: a
  recipe goal refuses with a sentence saying the count could not be read, a
  magazine with a real positive count beats one nobody could look inside, and a
  boredom goal — which never turned on the count — scores it on the factors that
  were readable instead of dropping it. A value present but not a number lands
  on unknown too. Absence refuses deliberately: a skill the character does not
  actually gain is worse than a book left unopened.

  Invisible until now because the shared fixture always writes the key, so no
  test could produce the payload the shipped mod actually sends — the same
  green-that-does-not-cover shape as the missing square tier, one field down.
  The fix was carried over from the mirror branch, where it was written beside
  P5 work that stays out of `dev`; it depends on none of it, and `literature.py`
  had not been touched here since the branches parted.

- **The vocabulary ledger went blind exactly where the seam got more careful**
  (`stabilize/arm-session-confirmation`). `_sidecar_keys` matched only the
  shared `read_*(payload, "key")` helpers, so the moment `unread_recipes` became
  tri-state — which *requires* bypassing those helpers, since they exist to
  supply the default — the literature block appeared to drop from eleven read
  keys to ten and `test_the_measured_counts_still_hold` failed. Nothing had
  stopped being read. Left alone, an extractor with that blind spot would retire
  a ledger row for every honest fix, which is the opposite of what the file is
  for. It now sees `payload.get("key")` as well.

  The ledger's own claim needed the same correction: membership means "read
  without a producer", which until now was the same thing as "decided on a
  default" and no longer is. `unread_recipes` stays listed — nothing produces it
  — with the set's comment carrying which rows still decide blind.

- **The handoff the game machine reads had fallen behind the findings**
  (`stabilize/arm-session-confirmation`). `LOCAL_GAME_HANDOFF.md` §4 opened with
  "three parts of the sidecar are wired to a mod that cannot drive them" and
  listed three. The ledger has held eight for several commits, and the three
  most consequential of this branch's discoveries were absent from the one
  document the person at the game machine actually reads before spending a
  session.

  That is the same defect this whole pass has been removing, aimed at the
  handoff: a document that was true when written, read as current later. It now
  lists all eight with what each looks like from the chair, and two of the new
  rows change what a session is worth spending on — `loot_area` is blocked twice
  over rather than once, and a food or drink *choice* is decided blind, so the
  act can be trusted as evidence while the selection cannot.

  The priority list gained the one experiment that would change what the agent
  is allowed to do: start a read or a meal, let a zombie approach, and confirm
  the character stops at all. §17.2's earlier interrupt is dead code, the flee
  rung above it is not, and only a live session can say whether that second rung
  fires when it must.

### Added

- **The STATUS choreography is in the working agreement instead of only in the
  tooling** (`stabilize/arm-session-confirmation`). `reconcile_status.py` was
  named in no document a contributor reads before committing — not `AGENTS.md`,
  not anything under `docs/` — so the sequence could be skipped without breaking
  a single written rule, which is exactly what happened: a code commit went out
  with `STATUS.json` still describing its predecessor, and CI refused it on both
  platforms while the local check had been green.

  The trap is that the gate is *already* in `scripts/check.sh` and would have
  caught it. Running the check before the commit passes it against a tree that
  no longer exists once the commit is made. `AGENTS.md` now carries the order —
  commit code, reconcile with the verdicts observed for the previous commit and
  the SHA each belongs to, run the check against the tree that will be pushed,
  commit STATUS alone, push — and the reason the STATUS commit must contain
  nothing else: the gate allows a later verdict-recording commit only while
  nothing outside `docs/control/` has changed.

  It also pins the flag that was missed on the first attempt at the repair. A
  reconcile that keeps an archive must pass `--rc-sha`, `--rc-run` and
  `--rc-sha256`, `STALE` ones included; omitting them nulls the RC's identity,
  which is not a modest claim but an empty one. `STALE` describes an archive's
  relation to the tree and does not retract its name.

- **A map of the contract seams, where the next person will look for it**
  (`stabilize/arm-session-confirmation`). `tests/contract/__init__.py` was
  empty; it now says which seams already have a standing agreement check and
  which one did not. Written because the previous commit was a duplicate
  checker that found nothing new and broke the existing one's Lua dumper on the
  way past — the map is the fix for that class of waste, not an apology for it.

  The inventory is worth stating on its own: agreement is already machine-checked
  for adapter arguments, capability declarations, capability evidence, the engine
  API inventory, the MCP tool surface, four wire schemas, and what the documents
  promise against what the parser does. Forty-seven contract tests in all. The
  one seam with no such check was the observation document's field
  vocabularies — and that is exactly where eight dead gates accumulated,
  including the three that cost the most: the agent cannot walk, nothing loots,
  and a safety rung that has never fired.

  That correspondence is the argument for the two files this branch added. It is
  also the general lesson, stated where a reader will meet it: an agreement kept
  only by review is kept until the day it is not, and the suite stays green
  through the whole of that day.

- **The rest of the seam checked, and the divergence turns out to be local**
  (`stabilize/arm-session-confirmation`). The vocabulary check now covers every
  structural tier crossing the observation boundary, and they all agree
  *exactly*: the item's own fields (12 keys), the container's (7) and the
  zombie's (6) match key for key — nothing read that is not sent, nothing sent
  that is not read. The zombie block matters most and is the most careful: both
  sides keep `visible`, `chasing` and `state` tri-state, with the mod recording
  an unknown in its limits rather than defaulting.

  That result changes the story the earlier findings told. The three blocks that
  diverged — `food`, `literature`, `fluid` — are exactly the ones passed through
  as raw `JsonDict` where nothing forced agreement, which is also why
  `schemas/observation.schema.json` declares them as objects and constrains none
  of their properties. Everywhere a typed dataclass faces an explicit Lua table,
  the two agree. So the repair is not "rewrite the contract" but "give those
  three the treatment the other tiers already have", plus the one unbuilt bridge
  for the weapon's condition — bounded work, still only confirmable in a live
  game.

  The check caught its author a second time: the item extraction read 8 keys
  instead of 12, because four of them are assigned after the table literal
  rather than inside it. A pattern that stops early does not prove agreement, it
  proves it looked at less.

- **The stats seam checked the same way, and it is clean** (`stabilize/arm-session-confirmation`).
  The item-domain vocabulary check was extended to the player's open stats map,
  expecting the item blocks over again. It is not, and a clean negative is worth
  a test rather than a shrug: every stat the sidecar reads — `endurance`,
  `fatigue`, `health`, `hunger`, `panic`, `thirst` — is one `Observe.playerStats`
  sends, and `observe.wounds_unknown` is minted by ObserveModel's limit block.
  Nothing there reads as a default for ever. `test_no_stat_is_decided_on_without_a_producer`
  now asserts that emptiness, so the disease stays confined to the item-detail
  blocks instead of spreading unnoticed.

  Reading the mod's own code also corrected a row this branch wrote last commit.
  `ItemView.extra["weapon"]` was described as the item-vocabulary mismatch again.
  It is not quite: `Observe.playerStats` puts the equipped weapon's wear in the
  stats map **deliberately**, saying why in a comment — "because the item tier
  has no condition field in the schema" — and refusing to fabricate a condition
  when the reader is absent. So it is one bridge that was never built, not two
  vocabularies drifting apart, and the ledger row now says so. (It also named
  `Observe.playerFields`, which is the wrong function; the stats are built by
  `Observe.playerStats`.)

- **The item-detail seam is now checked mechanically instead of one field at a
  time** (`stabilize/arm-session-confirmation`).
  `tests/contract/test_item_domain_vocabularies.py` reads the keys the mod's item
  readers emit, reads the keys the sidecar's typed views ask for, re-derives the
  counts `docs/LIMITATIONS.md` quotes, and pins the exact set of keys the sidecar
  decides on without a producer. A new mismatch on either side now fails a test
  rather than becoming the ninth thing somebody finds by accident. Three of the
  eight dead-gate rows were this one root, and `schemas/observation.schema.json`
  declares `food` and its siblings as objects while constraining none of their
  properties — nothing had ever compared the two vocabularies.

  The check earned itself immediately by failing on its author: the `fluid`
  set was written from memory and was wrong in three keys. Derived rather than
  recalled, it also sharpened a safety claim made two commits ago. Each hazard
  key crosses in exactly **one** block — `poisonous` is sent in `food` and not in
  `fluid`; `tainted` is sent in `fluid` and not in `food` — so poisoned food and
  tainted water are both still refused, but the crossed pairs are not. A fluid
  the engine flags poisonous rather than tainted reads as false on that key.
  Whether the game ever flags one that way is a live-game question; the earlier
  wording implied a symmetry that does not exist.

- **The sweep's last three claims, checked by hand: two real, one refuted, one a
  duplicate** (`stabilize/arm-session-confirmation`). Named as unverified last
  time rather than quietly dropped, so they were verified.

  `ItemView.extra["weapon"]` is real. `combat/policy.py` reads a weapon's
  condition out of an `extra` block the mod never builds — while the mod *does*
  read the condition, into the player's stats as `weapon_condition` and
  `weapon_condition_max`, which nothing on the sidecar reads. The
  item-vocabulary mismatch again. The direction is safe and the function says so
  itself: `None` means unreadable and the policy refuses rather than guessing, so
  an engagement stops at `weapon_unusable` instead of swinging a weapon nobody
  measured — and it sits behind `combat_assist`, which is experimental and
  unreachable anyway.

  `player.present == False` is a dead gate but a **benign** one, and that is
  worth a row precisely so the next reader does not mistake the gate for the
  mechanism. The mod cannot say it, but the condition arrives by another route:
  with no character, `Observe.context` returns nil and no observation is
  published at all, which the engine already treats as `GAME_DISCONNECTED`. The
  unusable-character case rides `alive`, which defaults the safe way.

  `chain.on_person == False` was not counted: it is a consequence of the
  world-container row already recorded, not an independent root — every
  container in the tree is on-person because no other kind ever enters it.

  Eight rows now. Both new patterns were checked in both directions, and the
  `present` one had to be tightened after the first attempt matched an unrelated
  `player_present = false` in the agent's own state — a false positive that would
  have made the row prove nothing.

- **A world container can be named but never resolved, so nothing loots**
  (`stabilize/arm-session-confirmation`). The third gap of the square tier's
  shape, verified by hand from the agent sweep's claims rather than taken on
  their word, and the one that takes a whole goal kind with it.

  `InventoryView.container` searches `inventory.containers` alone and
  `resolve_container` refuses `INVALID_REF` for anything not in it.
  `container.inspect` needs that twice — as a precondition and again to verify
  against the observation after — and `inventory.transfer` resolves its source
  the same way. The mod's inventory has exactly two roots, the main inventory and
  each worn container, with `CARRIED` containers nested inside items. There is no
  third root and no path that adds a nearby crate.

  The crate is not invisible: `buildObject` mints it a container reference
  whenever the descriptor carries an `object_index` and a `container_index`, so a
  planner can see it and name it. It simply cannot be resolved, because the
  reference points into a list it was never added to. With the missing square
  tier above it, the loot mission is blocked twice over — it cannot walk to the
  crate, and could not open it if it were standing there.

  Recorded, not repaired, and added to the dead-gate ledger with a pattern
  checked both ways: no match today, a match when a `WORLD` root is spliced in
  beside the `WORN` one. The missing half is a mod-side inventory tier for an
  open world container — when a crate enters the tree, when it leaves, what its
  contents cost to read every tick — and that is a contract addition whose only
  honest test is a live game.

- **The item-detail tier speaks two vocabularies** (`stabilize/arm-session-confirmation`).
  A deliberate sweep for the dead-gate class — the shape behind the missing
  square tier, five instances of which had all been found by accident — turned up
  the same failure one layer down, and this time the data is present under other
  names.

  Measured field by field, not estimated: `food` — the sidecar reads 22 keys, the
  mod sends 8, 6 agree; `literature` — 11 read, 5 sent, 2 agree; `fluid` — 16
  read, 3 sent, 1 agrees. `ObserveModel.domain` passes key names through verbatim
  and the typed views read the raw block straight off the observation, so the
  names have to match and mostly do not. The sharpest cases are one fact under
  two names: `pages` vs `pages_total`, `skill_level_min`/`max` vs
  `min_level`/`max_level`, `amount`/`capacity` vs
  `remaining_units`/`capacity_units`, and a boolean `rotten` against a
  `freshness == "rotten"` test.

  Nothing errors, because every reader defaults a missing key — so the decisions
  come out as though the world were uniformly bland. **`FoodView.is_rotten` is
  always false.** What does survive is worth naming precisely: `poisonous` and
  `tainted` are spelled the same on both sides, so poisoned food and tainted
  water are still refused; what is lost is rot, portions left, pages left and
  alcohol.

  Recorded in `docs/LIMITATIONS.md`, not repaired. Choosing which side renames —
  or adding a translation layer at the seam — is a contract decision across two
  languages whose only real test is a live game, and guessing it statically would
  be the same move as relaxing the sidecar to accept the square tier.

### Added

- **Two more rows in the dead-gate ledger, both found on purpose**
  (`stabilize/arm-session-confirmation`).
  `tests/contract/test_gates_without_producers.py` existed with three rows, all
  three found by accident while chasing something else. The comment audit turned
  up two more of the same shape — a sidecar gate whose producer was never
  written, behind a comment asserting it had been — so they are recorded where
  the test will notice if that ever changes.

  `ActionState.type` is the one that matters: §17.2's "interrupt a read or a meal
  when a zombie is near" has never fired, because the observation's action block
  is `Ownership.describe`'s table and that table has no `action_type`. The second
  is `dangerFloor`'s floor test, which reads a `position` sub-table its caller
  never supplies, so every zombie counts as same-floor.

  Both patterns were verified in both directions before being committed, which is
  the discipline the file itself argues for: neither matches the mod today, and
  each matches when a plausible producer is spliced in. The `action_type` window
  is measured rather than guessed — `describe`'s return table ends 784 characters
  into the function, the nearest unrelated `action_type` is 2296 in, so a 1200
  character window sees a real producer and not the other one.

### Fixed

- **The last six comment-audit survivors, verified and corrected**
  (`stabilize/arm-session-confirmation`). All six were false, none needed a
  behaviour change, and two turned out to matter more than prose.

  `ObserveModel.dangerFloor` documents itself as reading "the same fields the
  observation carries" and as counting zombies on another floor as present but
  never as closing. It reads neither: its only production caller hands it the
  raw reader table, whose zombies carry flat `x`/`y`/`z`, while the floor test
  reads `zombie.position.z` — so the guard's own `type(...) ~= "table"` branch
  fires for every zombie and a horde one storey up counts as closing. The error
  runs toward caution, which is why the docstring was corrected and the code was
  not: teaching it to read the flat `z` would make a safety guard *less*
  conservative on static reasoning alone.

  The wound reference is documented as having "no cross-language format to
  match". It has one: `policy.medical.wound_body_part` splits the string and
  reads segment three as the body part, yielding `""` — a part no command can
  name — for anything that does not split into exactly three. The comment
  invited precisely the change that would silently disable bandaging by
  location.

  The rest: `Counters:reset` is documented as called when a session is accepted
  and has no production caller at all, so sequence counters are game-session
  scoped rather than handshake-scoped; `PZAgent.Json` no longer refuses a
  corrupt byte string (it escapes it as Latin-1, which its own header calls a
  fallback), so the rule against splitting a long name now rests on honesty
  rather than on a lost observation; and `autonomy.py` twice justified an
  omission with "nothing in the protocol's action set" does this, when
  `medical.bandage`, `survival.rest` and `survival.sleep` have been there since
  P3, with goal kinds and care missions behind them. Both omissions stand — the
  autonomy table is the agent's own initiative, and the low-endurance case had a
  second reason that never depended on the false one — but they now say why
  truthfully.

- **A safety rung that has never fired, and a backstop that does not exist**
  (`stabilize/arm-session-confirmation`). The comment audit's remaining
  survivors were verified by hand rather than left on an agent's word. Three
  more were false, two of them in safety-relevant code, and **no behaviour
  changed** — each is corrected in place and the gap behind it recorded.

  `ReflexGuard` carries §17.2's "visible zombie near during read/eat →
  interrupt" as `DEFAULT_VULNERABLE_ACTIONS`, matched against
  `ActionState.type` under a comment saying the mod fills it with the action
  name it queued. The shipped mod never fills that field at all: the
  observation's action block is `Ownership.describe`'s table, which has no
  `action_type` — the only field `ObserveModel.action` reads into `type`. So
  `running_type` is always `""`, `vulnerable` is always false, and the rung is
  dead code. Where the mod does record an `action_type` it is the engine's Java
  class name kept for diagnostics, so wiring that through would not match
  either. The consequence is bounded, not absent: the flee rung above ignores
  the action type, so an emergency still stops everything — what is missing is
  the earlier reaction, at `interrupt_at` rather than `flee_at`.

  Two places promised that going stale disarms the mod, one of them inside the
  `DisarmNotice` text an operator reads. It does not: `Safety.sidecarStale` is
  consulted in exactly three places and all three only refuse, while
  `Safety.disarm` is reached only from a `session.disarm` command, a new
  session, or a panic stop. After an unclean exit whose disarm never lands the
  mod keeps reporting the mode it was granted — but cannot act on it, because
  `mayStart` refuses everything except stop, disarm and cancel while the
  heartbeat is stale. A stale reading, not a running agent, and now said that
  way. (The adversarial pass had waved the second one through as a terminology
  quibble; it is user-facing text naming a mechanism that does not exist, so it
  was corrected anyway.)

  Third, `combat_mission` sealed an owed retreat under a parenthetical claiming
  the refused-admission history also reaches that line "with nothing of ours in
  flight". It cannot: `_emit` sets `_pending_action` before the step is ever
  offered and the guard above returns on it — and since `5757008` that case
  ends the goal typed instead.

- **Four load-bearing comments that asserted a guarantee the code does not
  give** (`stabilize/arm-session-confirmation`). The mission-wedge defect fixed
  in `5757008` survived four passes because a comment made the broken path read
  as correct, so the same shape was hunted deliberately: comments that claim
  something about behaviour living somewhere else. Seven areas audited, thirteen
  candidates raised, three refuted on the spot, and every survivor re-checked by
  hand against the code it names before anything was edited. **No behaviour
  changed in this commit** — the comments were brought to the truth, and where
  the truth is a gap, the gap is now recorded instead of denied.

  The sharpest is not merely wrong prose. `movement.move_near` accepts
  `container`, `square` and `item` references and refuses `object`, justified on
  both sides of the seam by the claim that no scan can produce one
  (`movement.py`, and the mod's own `Movement.lua`, which had already replaced
  one wrong reason with another). Since `bf92ee2` the observer mints exactly one
  per-object reference — a door's, from its `object_index` — and `doors.py`
  requires that kind for the `door_ref` it resolves out of the same `nearby`
  block. So the one object reference the mod produces is the one `move_near`
  rejects, and **nothing walks the character up to a door**. The kinds were left
  as they are and the gap written into `docs/LIMITATIONS.md` beside the missing
  square tier: widening a contract on both sides to no observable effect, while
  the destination square a walk resolves against is still absent from every
  observation, would be a change nobody could confirm. `inventory.py` carried
  the mirror error, offering "a container reference is not [an object
  reference]" as the reason its prerequisite is a `move_to` — `move_near` takes
  a container reference and always has.

  The fourth is in safety code: the reflex guard's block rung explained itself as
  non-redundant because the mod "fills `safety.danger_level` from a value it
  never computes, so it is `none` while this is HIGH". The mod computes it —
  `ObserveModel.dangerFloor`, written at the end of every successful
  observation, which this same branch gave a measurement clock. The rung is
  still not redundant, for a reason that is actually true: the floor is
  deliberately coarse and derived from the zombie scan alone, while the rung
  reads the assessment the module builds.

- **A step the action channel refused wedged its mission until the goal's wall
  clock ran out** (`stabilize/arm-session-confirmation`). Found by a
  seven-way audit asking one question of every mission family: can a mission be
  driven to a state it never leaves without its goal ending? The answer for the
  families themselves was no — the queue's own `tick()` expires an abandoned
  active goal on `max_wall_ms`, and `runtime.tick()` calls it unconditionally
  before any acting — but the audit surfaced a path that is not a leak and is
  still a silence.

  `ActionChannel.submit` raises `LoopError` for two reasons it names itself: the
  queue filled between the wrapper's capacity check and the call — the MCP
  router submits to the same channel from its own thread — or an idempotency key
  was reused for a different request across a restart. All six
  `_submit_*_step` handlers caught it and returned, on a comment claiming "the
  step is dropped and the mission decides again from the next observation; its
  own bounds cap how often this can repeat". That comment was the journey
  path's, and it is false for a mission: the mission sets `_pending_action` when
  it *emits* the step, gates `next_step` on it, and only a terminal
  `ActionResult` clears it — a result that can never arrive for a request the
  channel never admitted. So the mission declined every later tick with none of
  its own bounds advancing, and the goal ended `ACTION_TIMEOUT` at its wall
  budget — up to fifteen minutes for loot — naming a timeout instead of the
  refusal that happened. This is the shape `fb8539a` already ended typed for an
  *evicted* step record, left behind on the branch beside it. All six now mark
  the mission abandoned and end the goal `CAPABILITY_UNAVAILABLE` with
  `UNADMITTED_STEP_DETAIL`, a separate sentence from `UNOBSERVED_STEP_DETAIL`
  because the two are different facts: there a record existed and was lost, here
  none was ever minted. The journey path keeps its `LoopError` handler unchanged
  — a journey really can replan past a dropped leg, which is why the comment was
  true where it was written. Red first: the failing test asserted the goal ended
  and got `ACTIVE`, and a second test states the wedge on its own by driving a
  mission six ticks past an unanswerable step.

- **Verified, not changed: two items the ledger carried as open.** Neither could
  be turned red, so neither got an edit. *Arming on a danger floor that was
  never measured* is unreachable for mutating commands on three independent
  links: `ActionEngine._drive` refuses to dispatch without an observation
  strictly newer than the last seen (`GAME_DISCONNECTED`), the mod publishes an
  observation only after `Safety.setDanger` runs, and `ObserveModel.dangerFloor`
  returns a `DANGER.*` constant on every path — including `HIGH` when nobody
  scanned — so `setDanger`'s early return on an unknown level cannot fire from
  that call site. *`_enforce_cap` dropping a live drive without ending its goal*
  needs a fifth live drive of one kind, while `MAX_TRACKED_* = 4` and the queue
  admits `DEFAULT_MAX_OPEN = 4` open goals in total; `app.py` builds its
  `GoalQueue` with that default and nothing overrides it, so the eviction cannot
  fire in the shipped configuration.

- **Thirty-four defects of one family: evidence read without checking whose it
  was, or when it was written** (`stabilize/arm-session-confirmation`). A static
  audit along the P0 causal chain — mod visibility, session identity, heartbeat
  freshness, the two-phase arm, terminality, pointer and sequence recovery, one
  action, goals made of several — starting from one found in `pz-agent play`,
  generalising its shape across the sidecar, and then running the same three
  families against the mod, which is where every one of the 2026-08-08 live
  findings had lived, and finally against the tri-state rule itself. Seventeen on
  the sidecar, seventeen on the mod. Every fix was
  watched red first; a hypothesis that could not be turned red was reported as a
  hypothesis and left alone.

  *Claims resting on evidence that did not prove them.* `play` confirmed its arm
  from any fresh heartbeat reporting armed in the requested mode, without
  comparing the session — so a heartbeat left by an **earlier** sidecar could
  confirm an arm this session never got, and the command exited 0. `doctor`
  PZD010 called a session active from an undated read, telling the owner of a
  game that crashed an hour ago that it was live, two lines under PZD006 saying
  that same heartbeat was stale. `HeartbeatMonitor` read a *future*-stamped
  heartbeat as fresh for the whole of the skew, so a peer whose clock ran ahead
  could publish once and stop forever while every reader saw "fresh" — the
  handshake and the snapshot reader already refuse a document stamped past their
  window; the heartbeat was the one with no ceiling in that direction. The
  `engage_single_zombie` mission reported a kill it had not made: a failed window
  plus a succeeded `combat.shove` — whose adapter verifies "down **or strictly
  further away**" — plus the zombie leaving the nearby tier closed the goal as
  done, the shove that pushed it away serving as the evidence. `avoid_threat`
  read a *missing* nearby tier as an open horizon, completing a retreat with a
  chaser four tiles out. `status` printed a silent heartbeat's armed/mode/player
  as the state now, three lines under the word "stale".

  *A previous run's document taken as current.* The snapshot reader compared
  session-scoped sequence numbers **across** sessions, so after a game session
  change it refused every new snapshot as a rewind and the sidecar went blind
  while handshake and heartbeat both looked healthy. A fresh reader also adopted
  the previous session's slots as its first observation — the picture an attach
  and an arm decision are made against. Both are now scoped to the session the
  sidecar handshook. An arm request stamped ahead of the tick's clock was
  consumed and armed the run, and a pid record stamped ahead read as a ticking
  sidecar forever: the same one-sided subtraction of two processes' clocks.

  *Work that reached no end.* A disarm superseding a pending arm countermanded
  nothing at the game, so the mod could finish arming into a mode the sidecar had
  abandoned. `disarm` stranded a suspended goal for ever — exempt from the
  pending TTL because activation was supposed to resume it, and activation
  refuses once disarmed — holding an open slot no timer could reach. A mission
  whose step record aged out of the channel's bounded history left the wrapper
  proposing nothing tick after tick, the goal idling to its wall clock; it now
  ends `CAPABILITY_UNAVAILABLE` on the next tick, identically across all six
  goal kinds. A journal recreated at an earlier serial now reports the loss
  instead of silently renumbering.

  *And the same three families on the mod side.* A running command had two
  bounds and both read one clock — the lease and the adapter timeout — while
  `now()` returns a constant on a build without `getTimestampMs`, which is one
  more Kahlua gap of the kind that live run hit; against a frozen clock neither
  can fire, so the adapter was polled without end and the sidecar waited on an
  ack nobody would write. A raise anywhere in admission killed the whole tick
  and left that command with **no ack at all, ever** — the reader has already
  tracked it, so every redelivery classifies as a duplicate, and duplicates are
  deliberately never acked. The trigger that made that reachable: registration
  validated an argument's type and enum values but not its numeric bounds, which
  are used only in a comparison against the arriving value, so a non-number
  raised at dispatch in a file whose contract is that malformed declarations are
  caught at load.

  The replay cache had no session dimension while outliving any one session, so
  a restarted sidecar reusing an idempotency key met its predecessor's stored
  result: `succeeded` replayed on evidence about objects whose runtime ids no
  longer denote the same things, **addressed to a session nobody is listening
  on** — so the live command went unanswered too. A session the mod had already
  closed could be reopened by re-presenting its own document. And the mod's
  armed state survived a session swap entirely: `Safety.disarm` was reached by
  an explicit disarm and by a panic stop and by nothing on a session change, so
  a second sidecar inherited authority it never asked for and could not see it
  held — it believes it is in OBSERVE while the mod accepts mutating commands.

  On the observation side: an unread body and an unhurt one produced the
  identical document (`wounds` is omitted when empty, and `treat_wounds`
  completes on exactly `bleeding_observed == 0`); the nearby scan reported a
  complete scan of an empty world when it could not read the world at all;
  `survival.rest` promoted a rest to succeeded with no departure reading; and
  `medical.bandage` verified on a dressing the wound was already wearing.

  *An absent reader is not a false answer — five more places.* The sharpest is
  safety-critical: the zombie scan returned an empty list on three failure paths
  (`getCell` missing, raising, or answering nil; `getZombieList` missing), all
  indistinguishable from an empty street, so the danger floor counted zero and
  answered `DANGER.NONE`. Three deterministic consumers act on that with no model
  in the loop — the mod's gate, the sidecar's threat abort, and the reflex guard,
  whose only "nobody could tell" channel is a table the mod always supplies. On
  any build where `getZombieList` is absent or renamed, an armed AUTONOMOUS agent
  was cleared to work, reason `POSTCONDITION_MET`, on a scan that never happened
  — the README's threat-interruption guarantee, on a count nobody took. The
  schema requires `danger_level` and has no absent form, so the floor now answers
  HIGH when the zombies could not be read (the lowest rung reaching both block
  thresholds: it starts nothing and interrupts a vulnerable long action without
  claiming an emergency nobody saw), with a `zombies_unknown` counter beside it
  so the HIGH cannot be mistaken for a measurement.

  Four more, one layer down, all from the same shared snapshot helper publishing
  an inventory nobody could walk as an empty one: `inventory.search` answered
  "you carry no bandage" about a character nobody read; `consume.eat`/`drink`
  read an item's absence from an unreadable inventory as it having been eaten;
  `equipment.unequip` read a double absence across a worn set and a hand that had
  stopped answering as a garment taken off; and `snapshotBody` flattened
  `bleeding` to false while `medical.bandage`'s whole postcondition is
  `bleeding == false` — the exact ack that file's own header says it exists to
  prevent. Each ends in the same place: the mod mints `verified` from any
  succeeded ack carrying evidence, so a verify concluding from a flattened
  absence promotes the capability in the very document the sidecar gates its
  write tools on.

  *A floor nobody has measured recently is not calm.* Closing the zombie-scan
  gap left its neighbour standing, and the neighbour turned out to be reachable.
  `Safety.setDanger` is called from exactly one place — the end of
  `Observe.context` — so every tick that fails *before* that point leaves the
  previous reading in `agent.safety.danger_level` while the mod keeps
  heartbeating and keeps accepting commands. It was reproduced end to end: arm an
  agent, observe once against a calm street (floor `none`), then remove `getCore`
  so `Heartbeat.detectBuild` fails, and tick twelve more times across sixty
  seconds with the sidecar heartbeat renewed and two chasing zombies now present.
  `Safety.mayStart(safety, "consume.eat", …)` answered **`POSTCONDITION_MET`** —
  cleared to act, on a reading a minute old, taken before the horde arrived. The
  floor now carries the clock that measured it (`danger_seen_ms`), and `mayStart`
  refuses a mutating action whose floor has not been re-measured within
  `DANGER_MAX_AGE_MS` (30 s, six sidecar heartbeat windows) with
  `PRECONDITION_FAILED` naming the missing *measurement* rather than a threat
  nobody saw. The refusal sits after the always-allowed and read-only returns, so
  `world.inspect` is never blocked and the state clears itself the moment one
  observation succeeds.

  *A square is asked about, not the first thing standing on it.* A water source
  is addressed by its **square** — `source_ref` is parsed as `RefKind.SQUARE` and
  the mod's `Consumption` adapter reads it back the same way and looks for water
  on that square — and `ObserveModel.buildObject` accordingly mints the square's
  reference for everything that is not a container or a door. One reference
  therefore denotes a place and everything on it, and the mod scans several
  objects per square by design. `consume.drink_source` resolved it with
  `nearby_object`, which is `next(o for o in nearby.objects if o.ref == ref)` —
  the *first* match. A tree scanned before the sink answered for the sink, and a
  square with a sink on it was refused `NO_SAFE_DRINK`, "nothing at
  square:…:1201:3400:0 reports water". The question is now asked of every object
  carrying that reference, and the refusal's evidence lists all their kinds
  instead of one; a square with nothing watery on it is still refused, which is
  the test that keeps the fix from becoming a formality. `nearby_object` keeps
  its single-match meaning for the callers that want it and now says in its own
  docstring why a property question needs `nearby_objects`.

  *A registered adapter that no test had ever built.* Chasing the reference
  defect above turned up `DrinkSourceAdapter` in a state nothing was watching
  for: registered on the dispatcher, exported from the package, offered as an MCP
  action — and constructed by no test anywhere in the repository. Its refusals
  and its postcondition had never run. A census of the registry found it was the
  only one of the twenty-six, so the gap is closed rather than wide, but it was
  found by hand while looking for something else, which is not a method.
  `tests/contract/test_registered_adapters_are_tested.py` now fails when a
  registered adapter is built by no test; it is checked non-vacuously (drop the
  consume tests and `DrinkSourceAdapter` is reported while `MoveToAdapter` is
  not), and it excludes itself from its own corpus so its failure message cannot
  count as the coverage it is complaining about. The adapter's own postcondition
  is now covered too — thirst falling proves the drink, an unchanged *or risen*
  thirst proves nothing, and the evidence still carries `source_ref` so an
  ordinary sip from a bottle cannot stand as confirmation of a capability nobody
  has seen work.

  *The planner was told a street was empty on a scan that never ran.* The mod
  publishes its own accounting of every reading it could not complete — eighteen
  counters under the `observe.` prefix in `player.stats`, which is an open scalar
  map, so all of them arrive on this side intact. **Nothing in the sidecar read a
  single one.** An earlier wave noted the declaration had no listener and left it
  as contract-shaped; the consumer that makes it matter is
  `compact_for_planner`, the only picture a planner is ever given, and it builds
  `stats` from a whitelist of five. So a zombie scan that could not run — the
  safety-critical case fixed on the mod side earlier in this pass, where the
  floor now answers HIGH — reached the model as `zombies: []`, `zombie_count: 0`,
  `zombies_truncated: false`: a positive claim that the street is empty *and the
  reading complete*, about a scan that never happened. The counts and truncation
  flags there are about what **this** side dropped, and cannot say what the mod
  could not read. The compact document now carries an `unread` block and the
  nearby tier a `zombies_unscanned` flag. The block is generic on purpose —
  enumerating the counters would leave the next one silently dropped, which is
  the state all eighteen were in — and it gets the treatment `moodles` already
  gets for the same reason, token-checked names and a cap, because it is an open
  map arriving from the game side. The compact document is serialised to the
  model wholesale, so this needed no prompt change to become visible.

  *A zombie whose intent nobody could read was assessed as calm.* The mod omits
  `chasing` when the build exposes no `getTarget`, and says so in its own
  comment: an absent accessor "must stay absent so the sidecar is told it could
  not be read". The schema agrees — only `ref` and `distance` are required. The
  sidecar's parser read that absence as `chasing=False`, a positive claim that
  the zombie is not hunting. `NearbyZombie` states the rule it broke in the file
  itself: `state` is `str | None` with a comment that an unreadable body state
  must never read as "standing", while `chasing` — which that class's own
  docstring calls more important than distance for the reflex guard — defaulted
  to `False`. The cost lands in `_zombie_level`, whose three ladders "differ by a
  full rung at every band": a chaser at contact range was assessed as an unaware
  zombie, one rung down, on every build without that accessor. The navigation
  executor lost the same fact — it adds `CHASING_STEP_COST` around a chaser's
  square, and an unread intent silently skipped it, routing the character past a
  zombie nobody could rule out. `chasing` is now `bool | None` end to end,
  omitted from `to_dict` rather than sent as a schema-invalid null; the threat
  ladder and the route cost both treat "could not say" as a possible chase, the
  same cost this pass already accepted for the failed zombie scan; and
  `chasing_count` stays a count of *observed* chasers, with
  `chasing_unknown_count` beside it, so the reason spoken to the player never
  says "three chasing" about one chaser and two the reader could not answer for.
  The local map's same-tick merge over the three states is explicit for the same
  reason: an observed chase wins, but an unread intent survives rather than
  collapsing to the calm neither reading claimed.

  *A body nobody could read was treated as a body with nothing wrong with it.*
  `is_bleeding` is `any(w.bleeding for w in wounds)`, and the mod publishes an
  empty wound list both when nothing is wrong and when it could not read the body
  — declaring the second case as `observe.wounds_unknown`. Three separate files
  cite "a missing `is_bleeding` never means 'not bleeding'" as settled practice
  while arguing for some *other* gate; `engine.py`'s multiplayer refusal invokes
  it by name. The gate it names was the one place not applying it. Two
  deterministic consumers acted on the difference: `assess_threat` skipped
  `bleeding_floor` entirely, so an unread body produced `DangerLevel.NONE` on a
  tick where the clock may already have been running; and `combat.policy`, which
  already refuses a fight on *unreadable health* — "an absent reader is never the
  good reading, for what a fight spends" — permitted one on an unread body,
  answering `SHOVE_FIRST`. Both now key on the mod's declaration through a new
  `PlayerState.wounds_unread`. The spoken reason keeps the two apart all the way
  to the words, the same way `chasing_count` does: "the player's body could not
  be read this tick" rather than a wound nobody saw. The planner sees
  `wounds_unread` beside `bleeding` in the compact view.

  The third consumer, `policy.autonomy`, is deliberately left: it *raises* a
  bandaging need from bleeding, so an unread body proposes less rather than more
  — the same do-less reasoning that left `player.moodles` alone.

  *The declaration reached the planner with no instruction attached.* Surfacing
  what the mod could not read — the `unread` block, `nearby.zombies_unscanned`,
  `player.wounds_unread` — was only half the job. This consumer is a language
  model, and an empty `zombies` list beside `zombie_count: 0` reads as an empty
  street whatever else is in the document; the prompt's rules, every one of them
  enforced by a parser, said nothing about absent readings. The deterministic
  guards refuse on their own, but the planner is what decides whether to cross
  open ground at all, so the rule it needed is not "refuse" but "stop concluding
  absence". `plan_instructions()` now carries it, naming the three fields and
  saying plainly that an empty list beside one of them means nobody looked —
  including the part that shows up in what a player reads: do not claim in the
  summary that something is absent when the reading for it was never taken.

  *A test that never ran.* An earlier hand-merge in this same pass appended the
  zombie-scan group to `tests/lua/test_observe.lua` **after** `Harness.finish`,
  which calls `os.exit`. Eighty-eight assertions — the ones covering the
  safety-critical fix above — were present, readable, named in the ledger and
  executed never; the suite reported 146 passing and exited before reaching them.
  This is the pass's own defect family turned on its evidence: a green count that
  did not cover what it appeared to cover. `finish` now sits at the end of the
  file and the suite runs 234.

  Two flattenings were examined and deliberately left: `player.alive` is false
  for both a corpse and a build without `isDead`, but every consumer refuses or
  stops on it and none acts, so the cost is a wrong sentence rather than a wrong
  action; `player.moodles` flattens an absent reader into "no moodles", whose
  only consumers propose *less*. Editing for either would have fixed nothing.

  Recorded and deliberately **not** fixed, a third door onto the same silence:
  the mission-cap eviction path marks a drive abandoned and banks its report
  without ending the goal. It fired in no test, and reproducing it needs a
  concurrency the queue does not currently produce.

  **One finding is reproduced, unfixed, and larger than a stabilization fix: the
  agent cannot walk.** `movement.move_to`, `movement.move_near`, `world.inspect`
  and the local map all locate the destination square by scanning
  `nearby.objects` for an entry whose `kind` is `square`, reading `loaded` /
  `blocked` / `closed_window` / `drop` from its semantics. The mod has no code
  path that emits one. `Observe.nearbyObjects` sets each entry's `kind` from the
  container type, from `getObjectName` lowercased, or to the literal `corpse`;
  `Refs.KIND.SQUARE` appears only where reference *strings* are minted and
  parsed; `nearbyFields` exports `objects` and `zombies` and no square tier; and
  the strings `"loaded"`, `"blocked"` and `"closed_window"` occur nowhere in the
  mod's Lua at all. Driven against a document assembled the way
  `Observe.nearbyObjects` assembles it, a one-square walk east and a walk up to a
  container the mod *did* report both refuse with `TARGET_NOT_LOADED` — "no
  loaded square was reported at (1201, 3400, 0)". Every navigation leg goes
  through this, so it is a total functional outage of movement, and it was
  invisible because the sidecar's fixtures mint the square objects
  (`tests/fixtures/adapter_worlds.py:a_square`) that the mod never sends: the
  same green-that-does-not-cover shape as the dead test group above, this time
  across a contract boundary. It is **not** fixed here. The sidecar side must not
  be relaxed — that is the forbidden direction, and it would walk the character
  onto squares nothing assessed. The mod side means building the half of the
  interface that was never built: a square record per scanned square, with a
  solidity read behind `blocked`, since emitting `loaded` alone would assert
  passability nobody measured. That is a scope decision, not a minimal edit, and
  it is written down for the owner rather than taken unilaterally.
  `TROUBLESHOOTING.md` carries the symptom so a live tester does not spend the
  session hunting their install.

  An implementation of the missing tier was built and adversarially verified, and
  it is **not** in this branch: three regressions were proven end to end against
  the mod's own published bytes. It fixed walking by breaking drinking —
  `buildObject` mints a non-container, non-door world object's ref with
  `Refs.buildSquare`, so every tree and water source inside the tier shared a
  reference with the ground under it, the square sorted first, and `common.nearby_object`'s
  `next(o for o in … if o.ref == ref)` returned the square: a sink one step away
  went from `consume.drink_source` accepted to refused `NO_SAFE_DRINK` on the same
  document. It published a wall as open ground on a build exposing `isSolid` and
  not `isSolidTrans`, because "at least one reader answered" was treated as "the
  question was answered" — the glass wall the two-reader design was justified by
  is published `['loaded']` and the walk into it is accepted. And it starved the
  planner one layer below the fix: `compact_for_planner` keeps the nearest 24
  objects in one merged list with one cap, so a separate square budget in the mod
  does not survive it — a furnished room lost nine real objects including a real
  door two squares east, and a warehouse aisle delivered zero squares to the
  planner. All three are recorded in `LIMITATIONS.md` with their measurements,
  because they are what the real fix has to solve; shipping the attempt would have
  traded a named refusal for two silent wrong answers.

  That fix closes the first of the three square-tier blockers, which changes what
  a tier now has to solve. Every other reference resolution against
  `nearby.objects` was audited and each asks a *position* question rather than a
  property one — `movement.move_near` in all four of its uses and the planner
  critic's `_destination` — and position is shared by construction, since
  everything answering to a square's reference stands on that square. The square
  lookups themselves match on kind and position, never on the reference. A
  regression test pins the exact case that killed the attempt (a `kind="square"`
  entry listed ahead of a sink at the same reference, drink still accepted) and is
  verified non-vacuous: restore the first-match resolution and it fails with the
  original `NO_SAFE_DRINK`. Two blockers remain — the partial solidity read and
  the planner's compact view.

  Sweeping the question that found the `chasing` defect — where does an absence
  become a positive claim? — across every parser turned up two more gates with no
  producer, the same shape as the square tier. `container.accessible` is **always
  true**: five sidecar sites refuse on it, and nothing anywhere in the mod ever
  sets that field, so a locked or blocked container is presented to the agent as
  reachable and all five refusals are dead. `observation.full` is always true, so
  `store.py`'s three partial-snapshot merge branches have never run — benign, but
  untested against anything real. Neither can be fixed here: both need engine
  readers of exactly the unverified kind that sank the tier. Three instances found
  by accident is the reason they are now a ledger rather than folklore —
  `tests/contract/test_gates_without_producers.py` asserts each producer is still
  absent, so implementing one fails the test and asks for the row to be moved
  instead of letting the dead branch wake up unnoticed. It carries a positive
  control, since a pattern language matching nothing would pass every row by
  construction.

  Two more are recorded with their reasoning rather than closed. The first is a
  **decision**, not a stabilisation fix: the age check above refuses a *stale*
  floor but accepts a floor that was **never** measured, because
  `Safety.newState` starts at `DANGER.NONE` with no timestamp and three suites
  arm without observing at all (`tests/lua/support/command_support.lua:201`,
  `test_action_runtime.lua`, `test_movement_runtime.lua`). Deleting one clause —
  `dangerAge ~= nil and` — closes it and breaks those three, which is a contract
  change about whether arming requires an observation. The options are (a) refuse
  a nil age like a stale one, (b) gate *arming* on a successful observation
  instead of the gate, or (c) accept it on the record; nobody has picked one, so
  it is written down here rather than chosen unilaterally. The second is a
  **wart** with its trace, so it need not be re-investigated: `nearbyObjects`
  fails the same silent way the zombie scan did, but all six sidecar consumers of
  `nearby.objects` abstain on an empty list rather than concluding from it —
  there is no local map that banks a scan as knowledge, and no explore or loot
  goal completes on the absence of a frontier or a container. The honest end has
  no listener, so declaring the gap would fix nothing today.

- **The first CI verdict this branch has ever had, and it is green on both
  platforms.** Making the workflows see `stabilize/**` produced a result rather
  than a promise: `python 3.11`, `python 3.12`, `lua` and `build artifact` all
  passed, and the Windows job built and certified a release candidate —
  `pz-agent-windows-v1.0.0-rc1.zip`, 48657492 bytes, 75 entries, sha256
  `9e84a4e5…` — the RC of *that* commit, kept here as history; the digest identifying the **current** archive lives in `docs/control/EVIDENCE_INDEX.md`, which is the one record that is supposed to answer "which RC" — with `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 8
  check(s) passed`. That includes `archive.bin: both executables are in bin/`,
  which the ZIP built in this Linux container could never satisfy and whose
  absence `LOCAL_GAME_HANDOFF.md` warns installers about; the packaged pair also
  completed an MCP `initialize` over the RPC link with `PATH` cut back to the
  system directories, and Windows ran 8073 of 8114 tests with no failures.
  `STATUS.json` now records `GREEN` on both platforms and the RC as `CURRENT` at
  this commit, with its real digest — the artifact API's `ed0467…` is the *upload
  wrapper's* hash, not the archive's, and writing that would have been a
  fabricated identity of exactly the kind this file exists to catch.
  `release_candidate.live_game` stays `NOT_RUN`, and the gate says so itself:
  "this says nothing about the agent having run inside Project Zomboid".
  `EVIDENCE_INDEX.md` moved with it — a guard nobody had exercised
  (`test_the_evidence_index_carries_the_digest_status_derives`) caught the two
  documents disagreeing about which RC they described, which is the drift that
  file exists to prevent.

- **CI had never run on this branch, and every STATUS entry said "pending"
  anyway.** Both workflows trigger on pushes to `main`, `dev`, `claude/**`,
  `epic/**`, `fix/**`, `rescue/**` and `swarm/**`, and on pull requests targeting
  `main` or `dev`. This branch is `stabilize/**`, and PR #10 targets
  `epic/ux-one-command-play` — so neither trigger fires, and the PR reports zero
  check runs across forty-three commits. Meanwhile every one of those commits was
  followed by a STATUS reconciliation recording both platforms as `PENDING`, which
  reads as "CI will tell us" about a verdict that was never coming. That is this
  pass's own defect family in its own control document, written by the same hand
  twenty-odd times. `stabilize/**` is now in both workflows' push branches, so the
  Linux suite and the Windows package build actually run here and `PENDING` means
  what it says. Nothing else changed: the local gate was always green, but a
  green gate on one Linux container is not the two platforms the STATUS file
  claims to be waiting on.

- **The README says the agent cannot walk yet.** Its status block was accurate on
  its own terms — "nothing in this README describes behaviour that is not backed
  by code and tests" is still true, because the movement code and its tests both
  exist — and that is exactly why the sentence was not enough. A reader deciding
  whether this build is worth their afternoon would not have learned the one fact
  that decides it. The block now says movement refuses every real observation and
  why, names what is unaffected (arming, the safety guarantees, the observation
  tiers, eating, drinking, equipping, bandaging, reading), and points at
  `LIMITATIONS.md` for the account and for the two other gates in the same state.

- **The live handoff no longer sends its reader into the one thing that cannot
  work.** `LOCAL_GAME_HANDOFF.md` is what the person with the game reads, and its
  section on what needs a real game said nothing about the agent being unable to
  walk. A tester would have installed the mod, armed, run the movement scenarios,
  met `TARGET_NOT_LOADED` on every one of them including the square next to the
  character, and had no way to tell a structural gap from a broken install —
  spending the only resource in this project that can produce live evidence at
  all. It now opens with the three gates that have no producer, what each looks
  like from the outside, and what to do about each (for movement: skip those
  scenarios, and do **not** relax the precondition to get past them). It also
  says what a session *is* worth spending on, in order: the 52 engine symbols
  first, because nearly every remaining unknown is downstream of them — including
  whether the square tier is buildable at all, since `isSolid` and `isSolidTrans`
  are still not rows in `GAME_API_VERIFICATION.md` and confirming them turns
  blocker 2 from a guess into a fact.

### Added

- **The character can build, and the check that matters is the one that refuses**
  (`epic/p5-crafting-building`, wave 2). Two actions: `building.inspect`,
  read-only and published on every install because it is what a user consults
  *before* granting the authority, and `building.build`, **P4 flat** — no
  escalation ladder, because there is no tier above it and no case where placing
  a permanent object is less than the top one. P4 has no autonomous path in any
  mode, so no arbiter, planner or initiative table can ever raise a wall; a test
  pins all three routes shut. There is **no demolition action**, deliberately:
  removing what somebody put there is a different authority and this build does
  not have it, which is exactly why the placement is refused so carefully.
  Refusals are typed and land before anything is queued — `SQUARE_OCCUPIED`
  naming what stands in the way (the agent never clears a square),
  `RECIPE_MATERIALS_MISSING` naming each shortfall, and **`WOULD_TRAP_PLAYER`**,
  the reason this wave exists: a bounded flood fill over the observed window,
  with the proposed structure treated as impassable, refuses any placement that
  removes the last route from the character's square to open ground. The bound
  is stated rather than hidden — the check cannot prove the character is not
  already enclosed by something beyond the window; it proves this placement does
  not remove the last exit it can see — and an unreadable map is a **refusal**,
  not a pass, because that is the case where a trapping wall is most likely.
  `build_structure` (16th kind) runs at most one attempt: a failed craft can be
  re-run because its materials are still countable, a failed build may or may
  not have placed something, so a second command would be a second irreversible
  attempt on the agent's own initiative. It reports `ENDED_UNCONFIRMED` instead.
- **The mod now describes the ground.** A bounded 7×7 window of squares is
  published in the vocabulary the sidecar already read (`loaded`/`blocked`/
  `drop`, plus a new `occupied`), which nothing had ever emitted. Every semantic
  is a positive reading: a fact no reader would answer produces no token at all,
  so "we could not tell" can never pass for "there is a way out".
- **A live route for both new capabilities.** `S21_CRAFT` and `S22_BUILD` join
  the playbook (22 scenarios), and S22 makes the `WOULD_TRAP_PLAYER` refusal an
  explicit operator step: ask for the wall that would seal the character in and
  confirm the refusal *before* asking for one that would not.

### Fixed

- **`craft_item` was unreachable through `pz_goal_submit` from the wave that
  shipped it.** The MCP router never read `product` or `count` out of the
  validated arguments, so every craft goal submitted through the tool was
  refused as missing the product the caller had supplied. Both are read and
  echoed now, pinned by tests on the typed request the channel receives.

### Documented

- **An `experimental` capability cannot be promoted by a live run** — and this
  had never been written down. `CapabilityReport.usable()` is false for
  `experimental`, the action engine refuses an unusable capability before
  sending anything, and `safety.disabled_capabilities` only subtracts; so the
  very run that would confirm `building`, `crafting`, `combat_assist`,
  `survival_sleep` or `drink_world_source` cannot be issued. The playbook,
  `COMPATIBILITY.md`, `LIMITATIONS.md` and `GAME_API_VERIFICATION.md` now say
  so, and the two new scenarios are shaped around it: reading halves run
  anywhere, write halves record `BLOCKED` with the reason rather than an
  invented PASS.

- **The character can make things** (`epic/p5-crafting-building`, wave 1 —
  crafting only; placing structures is a later wave and nothing here claims
  it). Two protocol actions: `crafting.inspect`, read-only on both sides
  because it reads recipe tables and materials already carried and moves
  nobody, and `crafting.craft`, behind a new `crafting` capability that starts
  EXPERIMENTAL — so the craft tool is *withheld* on every install until a live
  run promotes it, while the free inspection is published. One command crafts
  one item once: there is no loop in the mod, and a recipe that could run again
  is a report rather than a retry. Success is the product **observed** in the
  inventory afterwards with an ingredient observed to have fallen; a queued
  craft is never success. Refusals are typed before anything is queued —
  `RECIPE_UNKNOWN` names the token, `RECIPE_MATERIALS_MISSING` names each
  shortfall as held-of-needed — and a craft whose postcondition could not be
  read is not started at all. The 15th goal kind `craft_item` runs on a
  deterministic mission (bounded attempts, recipes and consecutive failures; a
  failed recipe is retired rather than re-run, because this is the first goal
  whose work cannot be walked back) and it does **not** go looting when
  materials are short — it fails honestly saying what is missing.
- **The risk tier follows the recipe, not the tool.** Crafting is P3 for
  spending what the character carries and escalates to P4 — no autonomous path
  at all — when the recipe may need a surface or a material line is only
  covered from a world container. Because `may_need_surface` is true unless the
  build positively says otherwise, on any install whose readout is silent every
  craft is P4. That outcome is stated in the catalogue, the tool docs and the
  knowledge corpus rather than left for a reader to infer from "escalates
  sometimes".
- **A crafting knowledge domain** — 13 rules, nine `verified_script` against
  real code and tests, four `unverified` about the game itself with wiki
  sources. Nothing is `verified_live`: this wave has no live evidence.

### Fixed

- **`learn_recipe` was dead end to end, and nothing said so.** The literature
  policy picked recipe magazines by reading `unread_recipes` off each item, and
  the mod's reader never published that key — so every real observation was
  rejected and the goal could not succeed on any machine. Three more keys were
  drifting the same quiet way: the mod wrote `pages`, `skill_level_min` and
  `skill_level_max` where the sidecar read `pages_total`, `min_level` and
  `max_level`. The reader now publishes the names the policy reads, counts
  unread recipes against the character's known set, and — this is the part that
  keeps it honest — reports the count as **absent** rather than `0` whenever it
  could not be established. A book that genuinely teaches nothing reports zero;
  a reader that could not tell says nothing, and the policy treats the two
  differently instead of quietly calling both "no recipes here".

- **A terminal is enough to play** (`epic/ux-one-command-play`, wave 1). The
  agent had a goal channel with two ways in — an MCP tool call, which needs a
  language model on a stdio pipe, and a Russian phrase, which needs a
  microphone — and a user with a keyboard could arm it and then had no way to
  tell it anything. Three commands close that: `pz-agent play` runs the whole
  cold-start sequence (validate, start the sidecar, wait for the game, arm) as
  one command, where every wait is bounded twice — a deadline *and* a poll
  count, so a stopped clock cannot hang it — and arming is granted only when
  the game's own heartbeat reports it in the mode that was asked for; a wait
  that runs out is a failure carrying what the heartbeat actually said, never a
  success in a quieter tone. It refuses in front of a panic latch with the arm
  path's remedy and never clears one, and it never touches the game process:
  launching Zomboid stays the user's, which is why the wait comes with
  instructions rather than a spawn. `pz-agent goal submit/status/cancel` sends
  the same typed `GoalRequest` over the same Core RPC link the MCP server and
  the voice companion use — the terminal is not a privileged caller, it meets
  the same 14 kinds, the same parameter ranges and the same refusals, with the
  valid values printed by the command that refused. There are deliberately no
  `pause`/`resume` verbs: touching the controls *is* the pause, and parking a
  goal so another may run belongs to the arbiter, which decides from an
  observation rather than from a command line. `pz-agent status --watch` keeps
  a compact HUD on screen — ANSI on a terminal, separators into a pipe, never
  an escape byte down a redirect.
- **The goal wire caught up with the goal model.** The remote codec now carries
  the suspension bookkeeping (`suspended_by`, `suspensions`,
  `active_ms_before_suspend`, `front_rank`), the two care parameters
  (`target_endurance`, `hours` — so `rest_until` and `sleep_until_rested` are
  submittable over the link for the first time), and the channel status tails
  (`progress`, `paused`, `report`). All of it decodes absence to the model's own
  defaults rather than inventing state, and every surface that renders it prints
  `unreported` for what the wire did not say — never `no` and never `0`, because
  "the agent is not paused" and "this build could not tell me" are different
  sentences and only one is safe to act on. `schemas/goal.schema.json` declares
  the four suspension keys it was closed against, with a conformance test on a
  record that actually carries them.

- **The character can defend itself — assisted, bounded, and never on its own
  initiative** (`epic/p4-assisted-combat`, wave 1). Four new protocol actions
  (`combat.equip_best`, `combat.shove`, `combat.engage`, `combat.retreat` —
  ActionName 27–30) under a new `combat_assist` capability that starts
  EXPERIMENTAL and can only be promoted by a live shove observed against the
  real game; the pre-existing `autonomous_attack` ceiling stays `unsupported`
  and is pinned untouched on both sides. `combat.engage` is one bounded
  window: 1–3 swings, hard 4-second wall clock, terminal with an honest
  reason either way — there is no loop in the mod. Sidecar-side
  `CombatPolicy` gates every engagement before a command is minted (group
  size over `max_group` — default 1, low endurance, panic, critical health,
  broken weapon are each a typed refusal, not a worse fight). The
  `engage_single_zombie` goal (14th kind) is deliberately parameterless —
  it re-selects the nearest live target every window rather than accepting a
  `target_ref` that could go stale into a kill order — and runs at most 4
  windows before honestly failing. Zombies now report a tri-state `state`
  (moving/prone/unknown) and the player's stats carry weapon condition, so
  "the swing landed" is an observed postcondition, not an assumption. The
  needs arbiter and autonomous initiative are pinned by test to never mint a
  combat action: in AUTONOMOUS mode threat still means avoid, never engage.
  (`epic/p3-survival-knowledge`, wave 1). `knowledge/gameplay/*.yaml` — 50
  rules across 8 domains, validated by `schemas/gameplay-knowledge.schema.json`
  and a loader that refuses claims dressed above their evidence:
  `verified_script` requires a code source whose repo path exists and test
  paths that exist (a deleted test demotes the rule loudly at load, not
  silently), `verified_live` requires a live evidence pointer, and PZwiki can
  never carry a verified status. The first corpus is distilled from the
  shipped code — 46 rules citing exact symbols and pinning tests, plus the
  honest split the directive demanded: "the code refuses rotten food" is
  verified_script, "rotten food sickens the character" is a separate
  wiki-sourced hypothesis. Bounded retrieval feeds the planner prompt only
  the rules relevant to the current goal, active needs and nearby objects
  (cap 12, ~4KB, UNVERIFIED markers on every hypothesis and every unverified
  number — the model must see which figures are guesses); a configured
  corpus that fails to load refuses the tick rather than planning without
  it. Three docs are *generated* from the corpus — `BEHAVIOR_REFERENCE.md`,
  `BUILD42_MECHANICS_SOURCES.md` (the provenance ledger), and the Russian
  `GAMEPLAY_AGENT_GUIDE_RU.md` — with a byte-drift gate in `check.sh`
  (`generate_knowledge_docs.py --check`), so code and prose cannot part ways
  quietly.
- **A need can interrupt the current goal — and give it back**
  (`epic/p2-goal-controller`, wave 3). The queue learned suspension without a
  new state: `suspend()` parks the ACTIVE goal at the front of the backlog
  with a marker, its wall budget stops burning while parked, and ordinary
  activation resumes it with exactly the remaining budget; a fourth
  suspension of the same goal is a typed refusal, so preemption cannot
  ping-pong a goal forever. On top rides the NeedsArbiter — AUTONOMOUS mode
  only, edge-triggered (a crossing, never a level): bleeding appearing
  outranks danger reaching HIGH outranks thirst outranks hunger past the
  policies' own critical lines. It suspends, injects the satisfy/care/avoid
  goal at the front, and on that goal's *any* terminal — success or failure —
  the original resumes mid-mission with its drive intact (a loot mission
  continues its candidate list after lunch). Every decision lands in a
  bounded ledger; a suspended goal shows its `suspended_by` through
  `pz_goal_status`. A restart mid-preemption restores the original as
  pending-with-marker while the in-flight preemptor honestly ends
  `SESSION_TERMINATED`.
- **Retreat is a route, not just a stop.** The local map remembers zombie
  sightings (bounded, decaying — stale threat is a guess either way, so the
  map errs toward caution within the horizon and forgets beyond it), and the
  route search tolls threatened squares so journeys detour around them —
  costs, never walls: a cornered character still finds the least-bad way out.
  `avoid_threat` (13th kind, speakable — «отступай», «беги») retreats to the
  nearest user safe zone or the square that maximises distance from every
  observed threat, and succeeds only on the observed postcondition: the
  nearest zombie at twice the threat ladder's close distance, or a safe zone
  with nothing chasing. Chasing threats at close range stay the reflex
  guard's band — no second driver under the wheel.
- **The mandatory survival chain runs without an LLM** (`epic/p2-goal-controller`,
  wave 2). `satisfy_hunger` and `satisfy_thirst` — speakable since the voice
  epic, LLM-served until now — are deterministic missions: read the stat
  (absent is a typed refusal, never zero), eat carried safe food first (the
  safety gates stay where they live, in the food/drink policies — the mission
  restates nothing), fetch from reachable containers when nothing is carried
  (memory-known category shelves first, nearest first, locked doors recorded
  as skips), transfer to main, consume, and claim success only when the stat
  is observed at the target. The user's reserves outrank hunger at any level:
  the agent fails typed before eating the strategic stock. Only the goal
  channel reroutes — the autonomy initiative path still asks its provider,
  and the asymmetry is documented in the contract test.
- **Three more care kinds** (12 total, nine deterministic): `treat_wounds`
  (speakable — «перевяжись»; bandages every observed bleeding wound
  worst-first, verifies each stopped from observation, honest partial failure
  when dressings run out), `rest_until` (target endurance, the rest adapter's
  own bounds), `sleep_until_rested` (the sleep adapter's danger>NONE refusal
  surfaces unchanged and is never retried into danger). Care missions carry
  phase tokens and sealed reports like the rest.
- **Goals survive a sidecar restart, honestly** (`epic/p2-goal-controller`,
  wave 1). One versioned `goals.json` beside the memory dir, written
  atomically at most once per tick and only when a goal actually changed
  state. On the next start the previous ACTIVE goal answers terminal
  `FAILED`/`SESSION_TERMINATED` — "the sidecar restarted while this goal was
  active" — never silence; PENDING goals come back under their original ids
  and idempotency digests (a resubmitted key resolves to the same goal, and a
  TTL that ran out during the downtime expires on the first tick). A corrupt
  file is set aside as `goals.json.corrupt` with a typed diagnostic, and the
  channel starts empty rather than guessing.
- **«Домой» is a word the agent obeys.** `return_home` (parameterless, the
  first new speakable voice goal since the voice epic) walks the character to
  the remembered home point through the deterministic navigation executor; no
  home set answers the exact remedy — "stand at home and run: pz-agent
  remember home". `explore_area` sweeps the unknown frontier of the local map
  within a scope (radius by default — exploring one's own room is a no-op),
  approaches each waypoint through a Journey, records locked doors as named
  skips, and claims `complete` only when no frontier cell remains. Nine goal
  kinds; four of them deterministic missions the LLM never touches.
- **`pz_goal_status` finally says what phase the work is in.** Additive
  `progress` (closed phase tokens + counters: approach/open/inspect/transfer,
  legs walked, waypoints visited), `paused` (the manual-takeover marker,
  invisible since the arm epic, now projected with its reason quarantined as
  untrusted text), and `report` (the loot/explore mission ledger, scrubbed,
  live or sealed). An LLM-served goal answers `progress: null` honestly — a
  deterministic phase is a claim only a deterministic server may make.
- **«Облутай квартиру» is now a typed goal** (`epic/p1-loot-area`, wave 2).
  `loot_area` (7 goal kinds) takes a scope — `room` (default), `building`, or
  `radius` 1..30 — plus `take_all` and an optional closed category list, and
  runs as a deterministic mission with no LLM call anywhere: pin the scope
  from the activation observation (a build that reports no rooms answers a
  typed refusal naming `scope=radius`, never a guess), discover world
  containers in scope, skip the ones the memory proves unchanged since their
  last inspection — recorded as a skip, not silently —, approach each through
  the navigation executor (a locked or barricaded door on the way becomes a
  skip reason naming the door), open, inspect, select by the deterministic
  loot policy (closed category vocabulary, user reserves outrank even
  `take_all`, greedy capacity fitting where an exact fit is a fit), and move
  items with `inventory.transfer_batch`. The mission ends `complete` only on
  the provable criterion — every candidate inspected or carrying a recorded
  skip reason — or `encumbered` the moment the main inventory provably cannot
  take the smallest wanted item. The terminal report survives in a bounded
  ledger: containers inspected, skipped with reasons, items taken per
  category, left per reason.
- **Observation grew rooms, buildings and corpses.** The player and every
  nearby object now carry tri-state `room`/`building` tokens (outdoors and
  "no reader on this build" are deliberately the same absence — a scope
  decision must not read the second as the first; names are normalised once,
  space to underscore, or dropped whole rather than mangled). Corpses with
  loot appear as `kind=corpse` container objects — observation-only for now:
  a dead body is not in the square's object list, so the world-container ref
  scheme cannot honestly address its inventory; the gap is recorded in
  `GAME_API_VERIFICATION.md` as a future protocol change, and the loot
  mission records such containers as skipped rather than pretending.
- **One command now moves a batch, and stopping honestly is part of the
  contract** (`epic/p1-loot-area`, wave 1). The dispatcher grew its first
  structured argument type: a declared, bounded LIST (dense array, 1..8
  elements, refs or plain strings, element-wise session checks, duplicates
  refused, a fresh sanitised copy handed to the adapter — and the
  type-dispatch fall-through that would have silently treated an unknown
  declared type as a ref is now a load-time refusal). On it rides
  `inventory.transfer_batch` (26 actions): up to eight items, possibly from
  different source containers, moved one at a time by the game's own transfer
  action with capacity re-checked before every enqueue. `succeeded` only when
  every requested item is observed in the destination; a capacity stop
  partway is a FAILED `CONTAINER_FULL` whose evidence carries the honest
  partial record — what landed, what stopped and why, what the batch never
  attempted. The Python half pre-checks the summed weights and, when only a
  prefix fits, refuses naming "the first k of n". MCP tool:
  `pz_action_transfer_batch` (41 tools).
- **The sidecar finally remembers what it saw in containers.** The
  long-orphaned `KnownContainer` store is now fed: every world container in
  an observation lands as a sighting, every enumeration (`container.inspect`,
  transfer evidence) lands as an inspection carrying `item_count` and a
  `content_revision` — a 16-hex change detector over the observed contents,
  documented as a detector, not an inventory. New queries for the loot
  planner: `container_unchanged(tail, revision)` (three-state honest: an
  empty revision is never "unchanged") and `uninspected_tails()`. Writes are
  batched (one flush per 10 s, plus shutdown), and a memory file another
  process wrote in between is re-read, never clobbered — the user's
  reservations outrank re-derivable sightings.
  square** (`epic/p1-doors-navigation`, wave 2). New `pz_agent_core.navigation`
  package: a bounded `LocalMap` (4096 cells, oldest-seen evicted first) that
  remembers only what observations proved — visited squares, obstacles, stairs
  as floor links, and doors as tri-state knowledge with their refs — and a
  `Journey` executor that plans A* legs bounded by the mod's 30-square walk
  limit, lets `allow_doors` handle closed unlocked doors in-walk, issues an
  explicit `door.open` only on the retry path after a door-shaped failure,
  folds `DOOR_LOCKED`/`DOOR_BARRICADED` answers back into the map and replans
  around them, and declares arrival only from an observed position — a
  `succeeded` move ack alone is never arrival. Every budget (search nodes,
  legs, replans, consecutive failures) is a typed refusal. `navigate_to` is
  now a first-class goal served entirely by this executor: the wrapped LLM
  planner is never asked (pinned by a spy in the contract test), and a loop
  with no planner configured at all still navigates. Voice deliberately
  refuses to carry it — coordinates are not dictated over a microphone, and a
  misheard digit walks the character somewhere else; the carve-out is
  import-checked. The remote RPC wire schema intentionally does not yet carry
  the kind (a `navigate_to` submission over RPC is a loud validation refusal,
  pinned both directions); local MCP and CLI serving is complete.

- **Doors are observable, addressable and operable**
  (`epic/p1-doors-navigation`). The 2026-08-08 live run found real doors and
  could decide nothing about them: the snapshot said only `kind=door`, the
  door shared a `square:` ref with everything else on its tile, and opening
  one took a human at the keyboard. Now: nearby doors carry `open`, `locked`,
  `barricaded` and `orientation` — each tri-state, absent when the build
  exposes no reader, because "the lock could not be read" and "unlocked"
  authorise different plans — plus their own stable `object:` reference. Three
  new protocol actions, `door.open`, `door.close`, `door.unlock` (25 total),
  ride a new `door_toggle` capability end-to-end: Lua adapter (walks into
  reach, toggles via the game's own `IsoDoor:ToggleDoor`, verifies by
  re-reading the door — a toggle the engine swallowed is `POSTCONDITION_FAILED`,
  never a claimed success), Python adapter (postcondition demanded from the
  *after* observation), MCP tools `pz_action_open_door` / `pz_action_close_door`
  / `pz_action_unlock_door`. A locked door answers the new `DOOR_LOCKED`
  reason code and a barricaded one `DOOR_BARRICADED` — distinct codes because
  they demand different replanning (a key hunt versus a detour). `door.unlock`
  demands an observably usable key and unlocks only through the game's own
  interaction, never by writing lock state.
- **`allow_doors` is now true, not just documented.** The move tools promised
  "may open doors on the way" while the flag was parsed sidecar-side and
  dropped. Both movement actions now declare and ship it (default true), and
  the mod honours it: a walk that stalls against a closed, unlocked,
  unbarricaded door toggles the door (verified by re-read), re-enqueues the
  walk, and records each opening in evidence — bounded at three doors per
  command. A locked or barricaded door on the route fails the walk with the
  door's own reason code naming the square. `allow_doors=false` behaves
  byte-identically to before, and a door whose state cannot be read is never
  touched.
- **`pz-agent latency` measures the P0 targets instead of estimating them.**
  A bounded reader joins the command and ack journals by `command_id` and
  reports exact nearest-rank p50/p95 for submit→accepted, accepted→started,
  started→terminal and end-to-end, plus observation cadence and heartbeat
  facts — labelling every cross-clock delta as such (the two processes' clocks
  are never corrected). `--targets` marks each P0 target MET/MISSED/UNMEASURED
  and never invents a number: a gameless machine reports UNMEASURED and exits
  0, and terminal-ack *visibility* is honestly UNMEASURED offline because no
  on-disk record carries the moment the sidecar read an ack. Live p95 numbers
  are the game machine's to produce.

### Fixed

- **The arm a client is told about is now the arm the game confirmed**
  (`epic/p0-windows-ipc-arm-recovery`). Live, `pz_session_arm` answered
  `armed=true` having armed only the sidecar; the game kept publishing
  `armed=false, mode=OFF` because no `session.arm` command was ever enqueued.
  Arming is now two-phase: the sidecar submits a real `session.arm` command
  through the queue and reports success only after observing *both* the mod's
  terminal `succeeded` ack *and* a fresh game heartbeat of the same session
  reporting `armed=true` in the requested mode — within a bounded window
  (default 5 s), after which the refusal names which half never arrived and a
  countermanding `session.disarm` closes the late-ack hole. Disarm stays
  locally ungated (user input wins) and notifies the game, surfacing an
  unconfirmed notification honestly.
- **A restarted sidecar is no longer a second producer.** The command queue
  seeded its outbound sequence at 0 on every start while the mod's journal
  still held records 0..N. The queue now recovers `highest+1` from the durable
  journal tail (bounded read, newest rotated generation when the live file is
  fresh, session-scoped), refuses with a typed error when bytes exist but
  nothing parses, and logs — rather than crashes on — a terminal ack for the
  previous process's in-flight command. In-process, a second `attach()` on an
  attached loop is now a typed refusal instead of a silent second
  `JournalWriter` over the same file.
- **The mod stopped colliding with everyone — including itself — on the
  snapshot pointer.** Live, the game repeatedly failed to open
  `observation.snapshot.pointer` for writing. The Lua IPC layer now retries a
  refused open boundedly (3 attempts, in-call), remembers the slot it last
  committed instead of re-reading the pointer from disk before every publish,
  carries a refused pointer commit over to the next publish (committed first,
  bounded at 10 publishes, and the pointer can never name a slot whose write
  failed), and reports a reader close that failed instead of discarding it.
  On the Python side, `read_json_document` gained a small read-side patience
  (4 × 10 ms — sized for a 125 ms tick, documented against the 0.5 s write
  budget) raising `SharingViolationError` so a locked file is distinguishable
  from a corrupt one, and `SnapshotReader` treats a locked pointer or slot as
  an honest per-poll miss that never regresses `_last_seq`. A two-process
  contention soak — a child process writing truncate-in-place exactly like the
  mod, torn slots included, against the real reader at 20 Hz — pins the whole
  protocol.
- **An action can no longer sit in `accepted` forever, invisibly.** New public
  tools: `pz_action_status` (a typed answer even for an id this sidecar no
  longer knows, naming the likely causes), `pz_action_await` (bounded wait for
  a terminal result; the name `pz_action_wait` was already taken by the
  in-game clock wait), and `pz_action_cancel_all` (mass cancel of mod-owned
  work only, idempotent, honest `null` for counts it cannot yet observe).
  `pz_session_status` now reports the game's own word beside the sidecar's —
  `desired_mode`, `effective_mode`, `game_armed`, and a tri-state
  `armed_mismatch` where "the game has said nothing" is not agreement. Every
  submission now carries a wall deadline (lease + grace) swept into a terminal
  `ACTION_TIMEOUT`, re-attach turns the previous attachment's records terminal
  instead of leaving clients polling `accepted`, and a manual takeover parks
  the active goal as paused-by-user rather than losing it (a new arm does not
  silently resume it).

- **The thirteen defects the first live session proved, fixed at their roots**
  (`epic/p0-build42-live-compat`; live run 2026-08-08, Project Zomboid Build
  42.20.2, Windows — the findings themselves are recorded in
  `docs/GAME_API_VERIFICATION.md`):
  - *The mod now appears in the Build 42.20 mod list.* `mod.info` declares
    `pzversion=42` (the real installer refuses `42.20`) and the empty
    `require=` line is gone. `TARGET_BUILD` stays `42.20` for the heartbeat;
    the new `MOD_INFO_PZVERSION` constant records the split, and the contract
    test pins both files to it.
  - *Adapters no longer depend on lucky load order.* Eight adapters
    (`Consumption`, `Containers`, `Equipment`, `Inventory`, `Literature`,
    `Medical`, `Rest`, `Sleep`) open with a statement-form
    `require "PZAgent/adapters/Toolkit"`; the dynamic-loading ban still holds
    (`require(` stays forbidden, and a new contract asserts every require in
    the mod names a `PZAgent/` module and nothing aliases the token).
  - *Kahlua has no global `next`, so the mod no longer calls it.* Every
    `next(t) == nil` emptiness check (`CommandDispatcher`, `ActionRuntime`,
    `CapabilityRuntime`, `adapters/Medical`) now goes through the new shared
    `PZAgent.Compat.hasEntries`, built on `pairs`, which the live game does
    provide. A contract test bans the global across the whole mod tree with no
    allowlist, and the ActionRuntime tests re-run the full command path with
    `_G.next` removed.
  - *An exception in the adapter lifecycle is now a terminal answer, not a
    wedge.* Live, `ActionRuntime.verify` crashed (on the missing `next`) after
    `session.arm`, the terminal ack never appeared, and the runtime hung on its
    current work forever. Every raise escaping `start`/`poll`/`verify`/
    `finalize` — and the runtime's own ack writes — now becomes a bounded
    terminal `failed`/`INTERNAL_ERROR` naming the phase, clears the in-flight
    slot, and leaves the runtime able to take `safety.stop` and the next valid
    command. An exception has no route to `succeeded`.
  - *The game now reads the session offer it was always supposed to read.*
    `pz_session_arm` armed only the sidecar; the game kept publishing
    `armed=false, mode=OFF` because nothing ever read `session.json`. The new
    `Runtime.readSession` reads the offer once per tick through the same `Ipc`
    primitive the heartbeat reader uses and feeds it to the session manager
    the mod always had (`Session.evaluate`: freshness, nonce replay, sidecar
    liveness). A nonce is only remembered once decidable — an offer rejected
    solely for a missing sidecar heartbeat is retried and accepted when
    liveness appears, which is exactly the ordering race the live run hit.
  - *A Russian item name no longer costs the whole observation.* Kahlua's
    `string.byte` on a Java string returns UTF-16 code units (Cyrillic "п" is
    `0x043F`, not two UTF-8 bytes), which the byte-oriented encoder refused.
    `PZAgent.Json` now classifies each string once — any unit above `0xFF`
    commits the string to the UTF-16 model, surrogate pairs combine, lone
    surrogates are refused by offset — while valid UTF-8 byte strings encode
    byte-identically to before and a lone high byte falls back to Latin-1
    deterministically. The overlong-encoding hole stays closed.
  - *The sidecar's atomic writer waits out Windows sharing violations.*
    `write_json_atomic` retries `os.replace` on `PermissionError` with the
    same bounded budget journal rotation uses (10 × 0.05 s), raises a typed
    `SharingViolationError` naming the target when a reader never lets go, and
    takes its scratch file with it on every failure path — including naming
    the leaked path honestly when even removal is refused.

- **Journal rotation no longer crashes on Windows when a reader is mid-poll.**
  `JournalWriter.rotate` moved and deleted files with `os.replace`/`unlink`,
  which on POSIX succeed under any open handle but on Windows raise
  `PermissionError` (WinError 32) when another handle holds the file — and the
  reader on the far side of every journal opens it for each poll. A rotation
  racing a poll took the CI soak down (`test_loop_soak`, and the second-process
  sidecar test through the same path). Rotation now retries each move on
  `PermissionError` with a small bounded budget (half a second worst case, zero
  cost on POSIX where the first attempt always wins), and a reader that never
  lets go becomes the writer's own `JournalError` naming the file rather than a
  bare crash. This is a real Windows defect, not a test-timing flake: the same
  identical tree was green on an earlier run only because no rotation happened
  to race a poll that time.

- **Three more input-boundary crashes closed, on the heartbeat/session, the
  observation diff, and the descriptor.** A third fuzz round found the same
  depth/number gap in every remaining place that reads JSON another program
  wrote: `ipc/atomic.py`'s `read_json_document` (the single boundary behind
  `HeartbeatMonitor` and `SessionManager`) and `observation/diff.py`'s
  `MappingDelta`/`ListDelta.from_dict` both recursed without bound and let a
  bare `ValueError` through on an absurd integer, and the descriptor loader
  could still overflow the parser on a file nested deep inside its byte cap.
  All three now measure nesting depth before parsing (`MAX_DOCUMENT_DEPTH` /
  `MAX_DESCRIPTOR_DEPTH`, both via the shared `pz_agent_core.jsonbytes`
  primitive) and refuse with their own typed error. Two seeded fuzzers
  (`test_session_handshake_fuzz.py`, `test_observation_model_fuzz.py`) join the
  suite. Two deeper issues the fuzz surfaced in `protocol/messages.py::_as_float`
  — an unguarded `float()` overflow and `allow_nan=True` letting `Infinity`/
  `NaN` through — are recorded for the protocol owner, not silently patched.

- **Five more crashes on hostile or corrupt input, across three boundaries,
  found by seeded fuzzers and all now typed refusals.** The same seeded-fuzz
  approach that hardened the RPC decoder was pointed at the other places that
  read bytes another program produced. (1) The **journal reader** — fed by the
  mod writing to disk — crashed with a `RecursionError` on a deeply nested line
  and a bare `ValueError` on an absurd integer literal, both under its line
  cap, where §3.5 promises a skipped "corrupt record"; it now bounds nesting
  depth before parsing and catches the broader `ValueError`, skipping the line
  with a diagnostic. (2) The **descriptor loader** — read at startup from the
  state directory — raised a bare `OverflowError` when a corrupt descriptor
  carried a pid past the platform's `pid_t` (`os.kill` overflowed), and a bare
  `TypeError` when `family` arrived as a JSON array or object (`x in {…}` on an
  unhashable value); both are now the loader's own `DescriptorError`. (3)
  `AgentConfig.to_toml` did not escape control characters, so a config with a
  newline in a free-form string field validated but could not re-load from the
  support bundle's rendered copy; it now escapes every control character. The
  depth-scan primitive is shared between the RPC decoder and the journal reader
  in the new `pz_agent_core.jsonbytes` module — one source of truth for the
  "measure depth before parsing, because catching `RecursionError` after the
  fact is not a recovery" rule.

- **Two decoder crashes on hostile RPC frames, both under the byte cap, both
  now typed refusals.** A seeded fuzz over `decode_request`/`decode_response`
  found that a frame of a thousand nested brackets (thirty-two times under the
  64 KiB request cap) overflowed the interpreter with a bare `RecursionError`,
  and an integer literal of five thousand digits raised a plain `ValueError`
  from CPython's integer-string-conversion ceiling — neither caught by the
  decoder, both reaching the serving loop raw. `_loaded` now measures nesting
  depth on the raw bytes *before* parsing and refuses past
  `MAX_NESTING_DEPTH` (so the parser is never handed a document that could
  recurse past the bound — catching `RecursionError` after the fact is not a
  safe recovery), and widens its catch to the `ValueError` the absurd number
  raises, naming both as `MALFORMED` without echoing the payload. The two
  reproducers are promoted from the fuzz suite's `finds_` markers to real
  regression tests.

### Added

- **A seeded, deterministic wire fuzzer and a bounded loop soak.**
  `tests/unit/test_wire_fuzz.py` drives thousands of structured mutations
  (truncation at every boundary, type swaps, absurd lengths, unicode and
  surrogate garbage, bounded deep nesting, duplicate keys) through both
  decoders, asserting every input either round-trips or raises a typed
  `RpcError` and nothing else — the property that surfaced the two crashes
  above. `tests/unit/test_loop_soak.py` runs a real `SidecarLoop` for
  thousands of ticks under a deterministic interleaving of observations, goal
  and action submissions, disarm/re-arm and panic, and asserts the design's
  boundedness invariants from the outside: the action record store never
  exceeds its cap and never evicts an in-flight record, the goal queue's
  pending stays within its cap, and the thread set returns to its start after
  a clean shutdown.


- **Remote actions are served over the link.** `action.submit` no longer
  refuses: a bounded `ActionChannel` (explicit caps, idempotent resubmission,
  frozen whole-replaced records) is drained one submission per tick on the
  loop's own thread through the real `ActionEngine` and its full safety
  machinery — a disarmed submission terminates as the engine's own refusal,
  a panic clears pending work naming the lever, and `action.status` answers
  the real record over the socket. Plans remain a *reasoned* refusal,
  recorded on the port: a synchronous multi-step plan on the tick thread
  would hold the stop levers hostage for its whole wall budget, and the
  served multi-step shape is the goal channel.
- **The wire speaks one language.** `action.wait` was unusable end to end
  (Python sent `game_seconds`, the mod demanded integer `duration_ms`, the
  units disagreed); the mod now measures the same world clock observations
  carry, counting a date change as exactly one midnight so a wait can finish
  late but never early. `plan.cancel` with a `command_id` cancels exactly
  the named command — in flight or queued — and refuses by name when nothing
  matches. The agreement contract that let both live now builds its registry
  the way `app.py` does, dumps the control adapters from the runtime's own
  table, carries zero exempt actions, and runs a two-way family census.
- **Movement survives reality.** Every walk that had ground to cover died
  `INTERNAL_ERROR` on its first poll (the bypassed declare-wrapper's
  `"running"` vocabulary), and an already-satisfied move failed its own
  postcondition (all-unchanged evidence with no way to say arrival is
  success). Both fixed at the adapter, both pinned by a runtime-level Lua
  suite that drives the real adapters through the real `ActionRuntime`.
- **Adversarial coverage across the stack.** Hostile RPC frames, replays,
  stale and wrong-server descriptors, mid-call death, restarts with key
  rotation, partial frames and dead-pid descriptors (15 new transport
  tests); six MCP adversarial paths driven against a live `pz-agent-mcp`
  child, each ending with a healthy follow-up call on the same process; and
  eleven executable pins over the Windows workflow, both PyInstaller specs
  and the evidence index, mutation-verified in both directions.
- **The map for the local agent matches the mod.** `GAME_API_VERIFICATION.md`
  rebuilt against the code: 195 swept symbols, zero missing rows, the five
  wrong rows (queue reader, `onSleep` argument order, `ISReadABook` arity,
  `getBodyParts`, `PlayerStats` spellings) now match their call sites, every
  row still `requires_live`.

- **The link is now proven across a real process boundary, three ways.**
  (1) `tests/contract/test_sidecar_serves_the_core.py` hands a genuine child
  interpreter nothing but the state directory; the child dials the descriptor
  cold and prints back the session id the loop's own attach minted in that run
  and the observation sequence number that travelled journal → store → link —
  facts an in-process client could have read from shared memory, and a second
  process cannot invent. Its negative companion runs the identical child with
  nothing serving and watches it refuse by name. (2) The Windows workflow
  gained `packaging/windows/prove_packaged_link.py`: the packaged
  `pz-agent.exe` serves the Core RPC link for real and the packaged
  `pz-agent-mcp.exe` answers a JSON-RPC `initialize` through it, driven with
  shipped flags only, every wait bounded, success only on the observed
  `serverInfo` result; the driver is exercised on Linux against the module
  entry points, both directions. (3) `scripts/check_release.py` now requires
  the MCP end-to-end suite by name in the JUnit report it certifies from —
  a report where `tests/contract/test_mcp_subprocess_e2e.py` never ran, or
  ran as skips, is refused, because aggregate counters cannot tell a full run
  from one where the suite that crosses the seam was deselected.

### Fixed

- **The Windows drop guard learned the platform's spelling of a hang-up.**
  Run 31247921064 showed the server-survival fix below was half right: a
  short idle budget does end the wait on an abandoned named pipe, but the
  hang-up surfaces from `connection.poll` itself as `BrokenPipeError` — and
  the poll sat outside `_exchange`'s recv-drop guard, so one vanished client
  unwound `serve_forever` and took the sidecar with it. On Unix the same
  fact arrives as `EOFError` from the recv, inside the guard. The poll now
  sits inside the guard, with a seam-pinning test driving the Windows
  spelling directly. The two remaining account-name tests that planted a
  `Users/<name>` segment under pytest's temp directory (mid-path past the
  stripped profile lead on Windows — out of the floor redactor's documented
  scope) now pin the collection report's spelling to the floor redactor's
  exactly, plus direct profile-rooted probes.

- **Three Windows-only regressions from the goal-channel/transport iteration,
  none reproducible on Linux, diagnosed from the CI log.** (1) The transport
  rework hand-dialled its socket and referenced `socket.AF_UNIX`
  unconditionally, so a Unix-family descriptor reaching the client on Windows
  crashed with `AttributeError` instead of reporting the sidecar unreachable;
  `_dial` now guards on `unix_socket_supported()` and raises the transport's
  own not-answering error, which the entry point maps to `EXIT_NOT_WIRED`.
  (2) The two new server-survival tests left the server's idle budget at 60 s,
  but a Windows named-pipe read begun before the peer vanished has no hard
  deadline — the documented asymmetry — so an abandoned handshake held the
  single serving thread past the follow-up client's patience; both tests now
  inject a short idle budget so recovery is observable on both platforms
  without weakening the survival assertion. (3) The new account-name evidence
  test buried a synthetic profile segment under pytest's temp directory, which
  the floor redactor deliberately does not reach (it targets profile *leads*,
  not mid-path `Users/` segments); the test now asserts the manifest carries
  the floor-redacted spelling and that a genuinely profile-rooted path has its
  account segment stripped.

- **The remaining criterion-coverage gaps are closed — and four criteria were
  false in code, now true.** Fifteen audit entries across four fronts: secret
  hygiene now scans REAL writers (a real token issued into a real state dir,
  the real support bundle built and every member's bytes swept; a full
  SidecarRpc lifecycle with all loggers captured; the three untested key
  shapes each removed by its own rule; `verify_bundle` over a
  credential-carrying archive); recovery now observed (the game dying
  mid-action ends GAME_DISCONNECTED; an established link dropped mid-exchange
  is survived on both ends; an unwritable state directory is *reported* — it
  used to raise and lose the session; a truncated journal now *refuses to
  arm* — it used to arm right over the tear); the release gate gained its
  missing teeth (an all-NOT_RUN manifest certifies nothing; a same-size
  member edit is caught by its digest; a member the index never recorded is
  refused — the gate used to certify an archive carrying an extra file); and
  the MCP subprocess E2E gained its two missing journeys (a real client
  submits, polls and cancels a goal against the real queue — which also
  found the fixture never passed its goal channel to the router; a sidecar
  lost mid-session is an error payload and the child survives). The plan
  gate itself gained two rules: archive tasks must transitively require the
  per-member verification, and no build task may PASS without a run to
  witness the build — the second caught a real instance the moment it ran.

- **The typed goal channel is served by the real sidecar.** `SidecarLoop` owns
  a `GoalQueue` ticked every loop tick (budgets and TTLs expire for real);
  armed and AUTONOMOUS, the loop activates the oldest admissible goal and asks
  the planner for a plan for *that* goal; a succeeded action with observed
  evidence ends it, anything else charges a step; a guard-forced disarm, an
  RPC disarm and shutdown all end the active goal through the queue's own
  vocabulary, and the panic sentinel empties the whole channel as a level.
  The goals port serves the queue's real admissions, statuses and
  cancellations under one documented lock seam — no IO, planner or engine
  call ever under the lock. Proven over the link: a client submits
  `satisfy_to=0.73` through `from_state_dir` and the loop's planner is asked
  for exactly that goal, lifecycle, duplicate detection, cancel and
  disarm-leaves-nothing all asserted with bounded waits.
- **Every wait on the peer process in the RPC transport is bounded — for
  real, per family.** The audit's finding that the criterion was partly false
  is fixed: a poll-guarded handshake on both families; on the Unix socket a
  deadline watchdog that severs the link mid-read, so a header-then-trickle
  peer ends within the call's budget; connect under `settimeout`; the
  server's accept-side handshake — an unbounded wait the audit had not even
  flagged, where a silent peer wedged the accept loop forever — now runs
  under the injectable idle budget; request read, reply write and idle wait
  each bounded; both `maxlength` caps now observed by tests that claim 1 GiB
  and send four bytes. On Windows named pipes a started read has no hard
  deadline — documented in the module docstring and pinned by a test, never
  claimed. An adversarial verifier mutation-tested every bound, found three
  claimed-but-unobserved ones and a per-family docstring overstatement, and
  fixed all four. The forbidden-pattern gate's eval/exec/pickle rules are now
  exercised over planted snippets and the shipped tree is swept unfiltered.

- **R-008 closed: the shipped sidecar now serves the Core RPC router over its
  real subsystems.** `pz_agent_cli/core_services.py` adapts the running
  `SidecarLoop` onto the CoreServices ports — session status, observations,
  capabilities, memory and diagnostics served from the loop's own immutable
  state (each read a single reference to whole-replaced frozen objects); arm,
  disarm and stop travelling the shipped one-slot control channel and panic
  sentinel with doubly bounded waits on the loop's own published decision.
  `pz-agent start` now serves the link between attach and the tick loop and
  withdraws the descriptor in the same finally as shutdown. Ports that cannot
  be served honestly yet refuse by name — `REMOTE_ACTIONS_UNSERVED`,
  `REMOTE_PLANS_UNSERVED`, the router's own no-goal-channel refusal — because
  a queue nothing drains would fabricate acceptance. Proven by
  `test_sidecar_serves_the_core.py`: the MCP client's exact
  `from_state_dir` path reaches the real loop over a real socket and reads
  the observation the fake mod wrote (fixture-chosen seq 47, no default
  produces it), and — after an adversarial verifier's mutation check found
  the shipped call site unpinned — by a test driving the real
  `start --foreground` and asserting the `rpc.serving` record.

### Changed

- **A criterion-coverage audit of the 75 heaviest claims moved the honest
  figure from 74.26% to 59.66%.** Twelve read-only auditors asked one question
  per weight-8+ PASS task: does the named test observe the stated criterion,
  such that the criterion becoming false fails the suite? 53 confirmed, with
  the observing assertion named. 22 refused — and among them the audit found
  the project's recurring defect live in the product: **the shipped sidecar
  never serves the Core RPC router** (R-008). `CoreRouter` is constructed
  only by tests; `pz-agent start` publishes no link, so a real `pz-agent-mcp`
  against a real sidecar finds nothing to connect to, while every E2E test
  hosts the router itself over fakes. The 22 tasks and their 34 ordered
  dependents are back to IN_PROGRESS (R-009), each with its precise gap
  recorded in `docs/control/evidence/criterion-audit-094cb8a.md`; each
  returns to PASS when an assertion observes its criterion.


### Fixed

- **The R-002 boundary tests hung windows-latest, and the suite gained the
  bound it preached.** The two new tests raised their exceptions through
  `asyncio.run` over a monkeypatched coroutine — Linux tolerated it, the
  Windows Tests step ran half an hour and never finished. They are rewritten
  loop-free: what they test is one `except` clause, which needs no event loop.
  And because a suite with no per-test bound violates the project's own
  "everything bounded" rule in the one place nobody had applied it,
  `pytest-timeout` (300 s per test, thread method) now turns any future hang
  into a named failure instead of a runner held to the platform's six-hour
  ceiling.

- **R-002, the last open remote blocker: a server crash is now a diagnosis,
  not a traceback.** An exception escaping the MCP server's build or serve
  loop — a catalogue defect, or an SDK that keeps its constructor signature
  and changes behind it — used to kill the child with Python's generic exit 1
  under a stack trace. `main` now reports it as `EXIT_SERVER_FAILED` (10) with
  one bounded stderr line naming the exception, stdout untouched because it
  belongs to the protocol even in death. `KeyboardInterrupt` deliberately
  passes through — the user's own hand is not a server failure. Every remote
  blocker in `docs/control/BLOCKERS.md` is now CLOSED.

### Changed

- **FINAL_IMPLEMENTATION_REPORT.md re-pinned to a green tree.** The report now
  states what `094af1e` actually measures: `scripts/check.sh` exits 0 (6559 of
  6564 tests passing, five named skips — one of them the plan's own "every
  remote task is closed"), both workflows green against the exact commit, and
  the artefact of record is CI's PATH-stripped, gate-certified archive
  (run 31227188006, sha256 `2d3d9e4b...`) rather than the incomplete
  Linux-built ZIP, which stays documented as what this container can and
  cannot produce. The release gate's refusal is now down to exactly the two
  missing executables — the boundary §9 exists to name.

- **The remote stage is complete: every remote-owned task and 48 of 54
  integration checks PASS; thirteen of fifteen epics close.** Each runnable
  check's command was executed and its outcome recorded with the observation
  it rests on; the CI-observed checks cite the green runs (31223693322,
  31225901032 — the latter answering both executables with PATH reduced to
  the system directories, so "the bundle needs no Python" is an observation).
  The six open checks and the two open epics are E14 and E15 — the live-game
  claims only a machine with the game can establish, and the plan's own skip
  message now reads "every remote task is closed; there is no next one to
  check". RB-003 superseded accordingly.

### Fixed

- **A plan regeneration wiped every check's evidence.** The generator carried
  a check's status but not its evidence, so the first rebuild after the
  checks were established left 48 PASS checks evidence-free and the gate
  refused the plan. Status and evidence now travel together, pinned by a
  regression test over the real plan.

- **R-007: the voice intent resolver existed twice, and the tested copy was
  not the shipped one.** `pz_agent_voice/intents.py` — 600+ lines production
  never imported — is deleted; `intent.py`, the module `session.py` actually
  runs, survives and now carries what the dead copy alone had: the percent
  sign as a spoken unit («поешь 80%»), the closed bare-number table that gives
  «прокачай механику до 7» its one honest reading, a defensive
  `IntentRefusal.INTERNAL` so a range-table drift becomes a spoken sentence
  instead of a `ValueError` quoting the spoken number, and import-time checks
  that every vocabulary word survives normalisation, no word is claimed by two
  tables or shadows a stop word, and every trainable skill has a spoken form.
  `test_voice_intents.py` is rewritten against the survivor; every behavioural
  claim that applied was kept or replaced by the survivor's equivalent, each
  fold recorded. An adversarial verifier mutation-tested the stop-first
  ordering (the reordered scan fails the test) and restored one dropped pin —
  a digit run past the length bound is refused before `int()` even when its
  value would fit.
- **The answer check now also proves the bundle needs no Python.** The
  `windows package` step that runs both executables does so with PATH reduced
  to the system directories, so an import that escaped the PyInstaller bundle
  fails on the runner instead of on a user's machine that never had Python.

### Added

- **The bridge protocol has a published schema.**
  `schemas/teamon_bridge.schema.json` states the wire contract a TeamON bridge
  implementer builds against: eight message types with their directions, the
  closed goal-token set, the outcome statuses, the handle shape and the
  utterance cap. `tests/contract/test_teamon_schema_conformance.py` holds the
  schema and `pz_agent_voice.teamon` together in both directions — every line
  the code can emit validates, every line the schema permits decodes — and
  compares the closed sets set-for-set so neither place can drift. The error
  branch deliberately leaves the fault-code set open: a reader must survive a
  code from a newer bridge, and refusing the report of a failure loses the
  failure.
- **A CI verdict survives its own recording.** The plan gate refused a
  STATUS.json claiming `GREEN` for any commit other than HEAD — an
  unsatisfiable rule, because recording a verdict requires a commit and the
  commit moves HEAD. It shipped and promptly refused its own recording commit.
  `GREEN` and RC `CURRENT` are now judged by the same predicate as the
  staleness rule always used: the verdict's commit must be an ancestor of HEAD
  with nothing outside `docs/control/` changed since. A code change still
  demotes the verdict to `STALE:GREEN` everywhere, generator and gate agreeing.

### Changed

- **The master plan reflects what a green `main` verified.** With both
  workflows green at `276b9d9` and the release candidate built from it,
  `scripts/verify_carryover.py` confirmed 133 tasks by running each one's named
  regression test here and now — the typed goal channel, the voice companion,
  the TeamON bridge, the RPC codecs and client, the MCP subprocess E2E surface,
  and the failure-recovery suite among them. Eleven CI-observed facts (the two
  executables building and answering on `windows-latest`, the archive being
  assembled, the Windows suite reproduced through Actions) are recorded with
  the run that observed them. Evidence pointers that predicted modules the
  architecture never grew (`session/holder.py`, `safety/stop.py`,
  `companion.py`) now name the modules the behaviours actually live in.
  Weighted progress moves from 24.87% to 53.25%; nothing was claimed whose
  test did not run, and the live-game fifth of the plan remains untouched at
  zero from this environment.

- **Local Core RPC: the channel that was missing between the two processes.**
  `pz-agent-mcp` is launched as a subprocess by an MCP client, so it never
  shares a process with the sidecar that owns the session, the observation store
  and the action engine. Until now it said so and refused to serve — the message
  `NO_SERVICES_MESSAGE` stated in its own words that this build had no channel
  handing the core to a second process. `pz_agent_core.rpc` is that channel: a
  Windows named pipe or a Unix socket, never a TCP address, authenticated per
  run by a 32-byte token in its own mode-0600 file, with a descriptor at
  `<state-dir>/runtime/core-rpc.json` that a client checks before using — format,
  protocol major, the recorded process still alive, and the token still beside
  it. A stale descriptor is refused rather than dialled: a pid can be reused,
  and then a client reads a *different* process's silence as the core's state.
  Documented in `docs/CORE_RPC.md` and pinned by two JSON schemas.
- **JSON on that link, never pickle.** `multiprocessing.connection` puts `send`
  and `recv`, which pickle, one letter from `send_bytes` and `recv_bytes`, which
  do not — and a pickle stream is arbitrary code execution in the process that
  reads it. Only the byte calls are used. The suite feeds a pickle whose
  `__reduce__` would raise on load, and poisons `Connection.recv`/`send` for the
  duration of a real call, so reaching for the convenient one fails in CI rather
  than in a user's process.
- **A weighted plan of record.** `docs/control/MASTER_PLAN.yaml`: 480 tasks in 15
  epics, five levels (EPIC → MILESTONE → TASK → CHECK → EVIDENCE), progress
  derived on every read as the summed weight of passing tasks over the summed
  weight of all of them. It replaces a model that counted steps, which said a
  paragraph of documentation and a live Project Zomboid scenario were the same
  size. Weight bands are validated, not advisory. Seven metrics are reported
  separately because a single figure hides a subsystem at zero — MCP and voice
  operability are both at 0.0% while Windows compatibility is at 89.3%.

### Fixed

- **The two CI workflows installed different projects.** `windows.yml` installed
  `.[dev,mcp]`; `ci.yml` installed `.[dev]`. `pz-agent-mcp` checks one thing
  before anything else — is the MCP SDK importable — and answers `EXIT_NO_SDK`
  (3) if it is not, so every assertion in `test_mcp_entry.py` and
  `test_mcp_subprocess_e2e.py` that runs the entry point and compares an exit
  code was comparing 3 against the code it meant to check. The same commit was
  green on windows-latest and red on ubuntu-latest with 34 failures, none of
  which were about the code they named; the local gate was green too, because a
  developer venv has the SDK in it. The fix is the extra. The guard is
  `tests/contract/test_ci_installs_what_the_tests_need.py`, which reads the
  extras out of both workflow files and requires them to be *equal* — the
  divergence, not the missing package, is what made the failure invisible — and
  proves its own premise by running the real entry point with the SDK shadowed
  by a package that raises `ImportError` and observing `EXIT_NO_SDK`.
- **The RPC token was written in text mode on Windows.** `os.open` defaults to
  text mode there, so `os.write` translated every `0x0A` in the payload into
  `0x0D 0x0A`. A token is 32 random bytes; the chance one of them is a newline
  is about one in eight. On those runs the file was 33 bytes, did not match the
  token the server was authenticating with, and the client was refused — on
  Windows only, one run in eight, with a message about authentication rather
  than about encoding. `os.O_BINARY` where the platform has it.
- **CI cloned shallow, so the recorded baseline SHAs could not resolve.**
  `actions/checkout@v4` defaults to `fetch-depth: 1`, and
  `tests/unit/test_control_baseline_evidence.py` resolves every recorded SHA
  with `git cat-file` — which is what makes that file evidence rather than
  prose. The objects were simply absent. Both workflows fetch the full history
  now, and the test says "this is a shallow clone" instead of "these SHAs are
  wrong".
- **`safety.disabled_capabilities`: switch a capability off by name.** The
  state `disabled_by_policy` existed, the mod guarded on it, `PermissionEngine`
  refused on it with a message written for a user — *"X is switched off by
  configuration"* — and `docs/COMPATIBILITY.md`, which ships inside the Windows
  archive, listed it as "available, but configuration forbids it". No
  configuration could produce it: the only constructor was called from three
  tests, there was no key to write, and unknown keys are hard errors here, so
  anything an operator invented was rejected. The page's own warning three rows
  below — that a panic stop cannot reach a sleeping character — gave a cautious
  reader a concrete reason to want the switch the same page described.
  Implemented rather than documented away, the way the multiplayer refusal was.
  Applied by the ledger rather than by editing the capability report, because
  the report is evidence about the install and a user's decision is not a
  finding about it; `status` reports a switched-off capability with that reason
  instead of dropping the name; an unknown name is a configuration error.

### Fixed

- **A path that mixed separators matched no redaction rule.** Spellings of a
  literal were enumerated whole — all-`/`, all-`\`, all-doubled,
  all-percent-encoded — so `C:\Users\Иван/Zomboid`, which is what
  `f"{path}/name"` produces, matched none of them and fell through to the
  shorter `home_dir` literal. Not a leak; the path was still struck out, but
  under `<USER_HOME>` instead of `<ZOMBOID>`, so the same file produced a
  different line on each platform — which is the one guarantee a placeholder
  exists to provide. Mixtures are exponential in the number of separators, so
  each position is matched independently now.
- **`portable_relative_path` was untestable, and that hid a defect.** It coerced
  both arguments with `PurePath`, which builds the *running* platform's flavour,
  so on Linux a `PureWindowsPath` was normalised before `as_posix()` ever ran and
  removing the call changed nothing any Linux test could see. It also made
  `portable_posix` return `C:\/Users/...`. The flavour is preserved now.
- **The credential tests asserted the placeholder appeared, never that the secret
  had left.** Those are different claims: shortening the value pattern to a
  single character inserts `<REDACTED>` in front of an intact key and satisfies
  every containment check in the file, while `findings()` returns nothing and
  `verify_bundle` calls the archive safe to share.
- **`ActionEngine`'s pre-flight manual-takeover guard had no failing mutation.**
  Deleting it left the whole suite green while the engine dispatched a command
  into a character the player had taken control of and then cancelled it — which
  is not the same as never sending it.
- **`pz-agent-mcp.exe` could not be built.** PyInstaller's `collect_submodules`
  discovers modules by importing them, and `mcp.cli` calls `sys.exit` at import
  time without its optional `typer` extra. `SystemExit` is not an `Exception`, so
  `on_error` could not skip it and the build died packaging a program that never
  runs a command line of the SDK's at all. `packaging/windows/specutil.py` reads
  the package directory instead.
- **The documented-command guard now covers `pz-agent-mcp` too.** It has its own
  parser — `--version` and `--describe` and nothing else — so a document naming
  a flag for it fails exactly the way `logs --redact` did, and the guard written
  for the first executable deliberately skipped the second. `configs/mcp/README.md`
  is a first-contact document for anyone wiring a client and prints several of
  these; they are parsed now.
- **A handoff document stated a count that this branch's own work changed.**
  `docs/LOCAL_GAME_HANDOFF.md` illustrated `live-test collect` with "copied 0,
  skipped 15"; wiring the trace made it 16. The sentence now states the
  behaviour — every missing file named, one line each, with counts — rather than
  a number that drifts whenever a file is added to the evidence.
- **`pz-agent start` no longer prints an MCP configuration naming a variable
  nothing reads.** The block it prints for pasting into a client set
  `PZ_AGENT_STATE_DIR`, a name that occurred exactly once in the repository — in
  the literal that printed it. `pz_agent_mcp` reads no environment variable at
  all, discovery reads `USERPROFILE`/`OneDrive`/`HOME`/`USERNAME`, and the
  server's parser takes neither a path nor a variable, so there was never a
  route for it. Meanwhile `configs/mcp/README.md` carries a section titled "Why
  `env` is empty" arguing that naming an unread variable "would look like
  configuration and be decoration", all three shipped client configurations
  carry `"env": {}`, and a test pinned exactly that — over the checked-in files
  only. The pin now covers the configuration the CLI hands a user, which is the
  one anybody actually pastes.
- **`docs/QUICKSTART.md` stopped telling a new user to command the agent by
  voice.** Section 7 named two routes for a first command and one of them is
  refused: this build carries `arm`, `disarm` and `stop` from a second process
  and has no channel that carries a *goal*, so a spoken "eat something" is
  refused and the companion answers «Не получилось.» The quickstart now says so
  and points at `VOICE.md`, which the archive now ships.
- **`voice run` writes the log the debug map sends an operator to.** Defect 18's
  shape, one package over: `docs/LOCAL_DEBUG_MAP.md` names `logs/` for both
  voice symptoms it lists — a phrase not recognised, and «стоп» heard while the
  character kept going — and the companion had never written a byte there. Its
  turn history and synthesiser failures sat in two bounded rings inside a
  process that then exited, while `VoiceCompanion.speech_failures` says in its
  own docstring that they are kept because "the companion went quiet" with
  nothing recorded is what a support bundle cannot explain. Written at the run's
  edges into the same rotating file the sidecar uses, so both halves of "did the
  stop I said reach the sidecar" end up in one place in order. **Intents and
  outcomes, never transcripts** — a bundle is designed to be attached to a
  public issue and a microphone's contents do not belong in one.
- **`installer/` says what it is.** A complete, tested, 927-line standalone
  installer with a guide titled "Installing pz-agent on Windows", reachable from
  nothing, in no shipped artefact, and read as *the* install instructions by
  anyone who opens the directory. The shipped path is `install.bat` →
  `pz-agent install-mod`; a checkout follows `docs/QUICKSTART.md`. It is kept —
  it is the only path that works before anything is installed — and the guide
  and the module now open by naming which of the three cases each is for.
- **AGENTS.md and CONTRIBUTING.md claimed an enforcement that did not exist.**
  Both said `scripts/check_forbidden.py` fails the build on an empty exception
  handler. It had no such check, for exactly the handler style this codebase
  writes, so a rule two governing documents declare binding was unenforced and
  unreviewed. It cannot be scanned honestly either: the tree contains an
  `except (OSError, UnicodeDecodeError): pass` that falls through to a second
  lookup, and several `except OSError: return` that deliberately trade a
  diagnostic for a session in flight. So the part that *can* be scanned now is —
  an untyped `except:`, which also catches `KeyboardInterrupt` and `SystemExit`
  and of which the tree has none — and the swallow is stated as a review rule
  with the reason it is one. `check_forbidden.py` had no tests at all; it has
  them now, including both directions of every rule the documents promise.
- **Every link a shipped document makes now lands inside the archive.** Defect
  13 was one instance of this — two documents the archive omitted while its own
  shipped documents told an operator to open them — and the fix was two names in
  a tuple, which left the general case untouched. Seven of the archive README's
  links resolved to nothing: `CONTRIBUTING.md`, `AGENTS.md`,
  `docs/ARCHITECTURE.md`, `docs/PROTOCOL.md`, `docs/TESTING.md`,
  `docs/DEVELOPMENT.md` and the blueprint directory, plus `PROGRESS.md`'s link
  to the task graph and `PROTOCOL.md`'s to `schemas/` and `tests/contract/`. An
  operator on Windows has no repository, so a relative link is either a file
  beside the one they are reading or nothing at all. `ARCHITECTURE.md` and
  `PROTOCOL.md` now ship — the second is what `LOCAL_DEBUG_MAP.md` and
  `LIVE_TEST_PLAYBOOK.md` assume when they discuss journals, refs and recovery —
  and the links about *building* the project became absolute, so a GitHub reader
  follows them and an operator gets a URL rather than a dead path.
- **`game.install_dir` and `game.user_dir` now do something.** Both were parsed,
  validated, typed and read by nothing, while `doctor`'s own remediation for
  `PZD001`, `docs/TROUBLESHOOTING.md` for `PZD001` and `PZD003`, and
  `configs/mcp/README.md` all told a blocked user to set them. Those two
  failures brick every other command — a GOG or manual copy Steam does not
  list, a profile moved by OneDrive or `-cachedir` — so the one documented
  escape hatch produced "configuration is valid" and then the identical failure
  telling the user to do what they had just done. Discovery now runs a second
  pass with the configured paths. Precedence is command line, then
  configuration, then discovery. A configured path that does not exist is
  reported *at that path* rather than falling back to a search, so a typo is
  visible instead of hidden behind the original error.
- **`safety.panic_hotkey` no longer accepts a value it cannot bind.**
  `PZAgent_Main.lua` binds DirectInput scancode 88 directly and reads no
  configuration, so every value other than `F12` bound nothing — and this is the
  stop button. A user rebinding away from F12 (Steam's default screenshot key,
  so there is a real reason to) was told "configuration is valid" and had bound
  nothing at all. Any other value is now a hard error naming the two routes that
  do work: `pz-agent stop`, and the `panic.stop` sentinel. Rebinding for real
  needs the mod to read a published keycode *and* a live run to prove the new
  key reaches the stop; until both exist, saying so is the honest answer.
- **The mod can publish `experimental`.** `CapabilityRuntime` reads
  `adapter.experimental` and `Toolkit.declare` never carried the field, so it
  was read in one place and written in none — the same shape as the very first
  defect on this branch. Two adapters carried comments saying "the probe caps
  this at experimental" and both published as ordinary `available_unverified`,
  while `docs/PROTOCOL.md` documents `capabilities.json` with an example showing
  a state its own writer could not emit. `survival_sleep` and
  `drink_world_source` declare it now.
- **Three documents told users to run commands that do not exist.**
  `SECURITY.md` — the page a vulnerability reporter lands on — said to check a
  support bundle with `pz-agent logs --redact --verify` before attaching it to a
  public issue; there is no `--redact`, so the single gate between a reporter
  and an unredacted archive was an instruction that exits 2. `PRIVACY.md` said
  `pz-agent memory --forget` clears the memory store; there is no `memory`
  command, and the real one — `remember forget` — appeared in no document at
  all. `docs/TROUBLESHOOTING.md` sent a user to `pz-agent status --explain` for
  the food policy's rejection list, and additionally said the thresholds are
  "in configuration" when `[safety]` holds four keys and none of them is one.
  All three corrected, and `tests/contract/test_documented_commands_parse.py`
  now puts every `pz-agent` command line any shipped document prints through
  the real parser — it is what found the third one.
- **The reflex guard's comment described the opposite of the running system.**
  `ReflexConfig.block_at` said the engine's threat threshold and its own compare
  against two inputs of which "only one is filled in by anything". Both are
  filled in and both are live: `Observe.lua` sets the danger floor from the
  squares around the player, and the guard takes the higher of that and its own
  assessment. A maintainer trusting the comment would have concluded
  `ActionEngine.threat_threshold` was dead configuration — and it is the only
  thing that interrupts a two-minute `literature.read` when a zombie closes,
  because the guard cannot run while the engine holds the tick.

- **The sidecar now writes the log nineteen live scenarios tell an operator to
  collect.** `DiagnosticLog` was complete — rotating, redacting, level-filtered,
  well tested — and constructed nowhere outside the test suite, so
  `logs/pz-agent.log` and `logs/pz-agent.jsonl` did not exist and could not.
  Nineteen of the twenty scenarios name the first among the files to collect and
  three name the second; `docs/LOCAL_DEBUG_MAP.md` sends an operator to it by
  name; `pz-agent logs` reads it; `logs --bundle` packs its directory into the
  archive `docs/TROUBLESHOOTING.md` asks a user to attach to a report. Four
  documents and twenty scenarios rested on a file the product never produced,
  and `live-test collect` had been reporting "copied 0 file(s), skipped 15" the
  whole time. `pz-agent start --foreground` now records the attach, the run's
  end, every retained safety event and the shutdown. Writing is at the run's
  edges rather than in the tick, and every write is optional and guarded: a log
  directory that will not take a file costs the log, never the session.
- **`pz-agent replay` has something to replay.** `TraceWriter` had the same
  defect and one more document on top of it: `docs/QUICKSTART.md` printed
  `pz-agent replay <trace>` under "When something goes wrong", `logs --bundle`
  packed `traces/*.jsonl`, and nothing had ever written a trace. The sidecar now
  records each observation — a full snapshot first, then diffs against it — and
  each action next to the terminal result that closed it, at
  `<state>/traces/session.jsonl`. Closing it needed a seam rather than a call:
  `ActionEngine` returns a result and never let go of the command it sent, so it
  gained an optional `on_dispatch` observer and the loop pairs the two. An
  action refused before dispatch is recorded with its reason and no command,
  because that is the case an operator is most likely to be reading a trace for.
- **`live-test collect` takes the trace, which no scenario knows to ask for.**
  `collect` builds its file list from each scenario's declared `logs`, and all
  twenty of those lists were written when nothing in the product produced a
  trace — so the newest piece of evidence would have stayed in the workspace
  while `docs/LOCAL_GAME_HANDOFF.md` told an operator to replay it from the
  evidence. The current file and every rotated generation are now copied into
  the scenario's `logs/` unconditionally, alongside the journals and snapshots
  that are collected the same way. The current file is named rather than
  globbed, so its absence is *reported*; the rotated generations are globbed,
  because a scenario short enough not to rotate is not missing anything.
- **A rotated trace stays replayable from its first line.** Found by writing the
  first one: `replay_observations` refuses an observation diff it has no
  baseline for, and a rotation that fell on a diff put one at the top of the new
  file — so every run long enough to rotate would have produced a trace that
  read back as a refusal. `TraceWriter.record_world` now asks whether a diff
  would rotate the file and writes the snapshot instead, letting the *snapshot*
  trigger the rotation and open the new file with what a replay needs.

### Changed

- **`docs/RELEASE.md` asks for the evidence the executable gate checks.** Its
  evidence checklist required "Game smoke evidence — S01–S15" from
  `tests/game-smoke/` and never mentioned `release/evidence-manifest.json`,
  which is the only thing `scripts/check_release.py --release` actually looks
  for. A human working the checklist and a machine working the gate were
  checking different things. The checklist now names the manifest, and states
  plainly that two scenario catalogues exist with colliding numbers —
  `S06_drink.yaml` in one is `S06_MANUAL_TAKEOVER` in the other — so a
  scenario id is ambiguous unless the catalogue is named with it.
- **Protocol 1.0 → 1.1.** The action whitelist grew from fifteen names to
  twenty-two, seventeen of them owned by the mod's adapter files. Added:
  `container.inspect`,
  `container.open_nearby`, `inventory.search`, `medical.bandage`,
  `survival.rest`, `survival.sleep`, `consume.drink_source`.
  `container.open_nearby` is deliberately not
  read-only — opening a container is a timed action the character performs, so
  placing it beside `world.inspect` would let an unarmed session move the
  character.
- **`inventory.equip` and `inventory.unequip` are now `equipment.equip` and
  `equipment.unequip`.** A rename, not an alias: the dispatcher's whitelist
  decides what may reach an adapter at all, and two spellings for one action is
  a second door. `SCHEMA_VERSION` stays at 1.0 — the document shapes did not
  change, only an enum inside them gained members.

### Added

- **`consume.drink_source`: fill a vessel at a sink, well or rain collector and
  drink from it.** The mod could already do this, behind an optional
  `refill_from` argument on `consume.drink`; the sidecar had no argument for it
  at all, so the path was unreachable from Python. Worse, it ran under
  `drink_carried` — a capability a static scan verifies — while §12.4 caps
  `drink_world_source` at `experimental`. Splitting it into its own action makes
  the gate structural: the engine reads `required_capability` from the adapter
  that owns the action, before that adapter is entered. `consume.drink` now
  refuses a world-source argument rather than honouring it, and the world-source
  postcondition accepts only thirst — a refill raises the vessel's volume and
  the drink lowers it again, so the vessel witnesses nothing in either
  direction. Published as `pz_action_drink_source`.
- **The support bundle's verifier no longer flags its own redaction.**
  `docs/TROUBLESHOOTING.md` tells a stuck user to run
  `pz-agent logs --bundle --verify` before attaching an archive to a public
  issue, and the whole point of `--verify` is to answer whether anything
  private survived. The `credential_assignment` rule matched
  `api_key=<REDACTED>` — its value group accepts the placeholder the rule
  itself writes — so the command printed "REVIEW BEFORE SHARING" and exited 1
  over a line whose secret had been correctly struck out. `text` was
  unaffected; `findings` is what the verifier asks. Nothing leaked, and that is
  not the harm: a verifier that flags its own success teaches an operator to
  ignore the next flag, and the next flag is the real one. Every rule is now
  checked against every placeholder this module writes, not only the one that
  bit, and redaction is asserted stable under a second pass.
- **`configs/mcp/README.md` names both refusals a client can meet, not one.**
  It said `pz-agent-mcp` "starts, finds no core services attached to its
  process ... and exits with status 1". On a plain install you get **3**,
  because the SDK gate fires first and its message is about a missing optional
  extra rather than a missing sidecar. The exit codes are deliberately distinct
  — `EXIT_NO_SDK` exists precisely "because the remedy is a single install
  command" — and documenting only the second sent a client author after the
  wrong cause on their very first launch. Both are described now, in the order
  they fire. `tests/contract/test_mcp_exit_codes_documented.py` pins the stated
  codes to the constants and to a real subprocess launch, and exercises
  `--describe`, which is the one thing that document promises works with no
  game, no sidecar and no SDK.
- **`pz-agent start` confirms the sidecar is still there before reporting one.**
  It returned success as soon as `Popen` returned, which reports that a *fork*
  succeeded and nothing about whether the program ran. A sidecar that died on
  its first import left `start` printing "sidecar started as pid N" and exiting
  0; `arm` then failed for reasons that named nothing, and `stop` said "the
  signal could not be delivered (No such process)" and exited 0 as well. The
  spawner now watches the child for `SPAWN_GRACE_S` and, if it is already gone,
  raises with the exit code and the tail of the spawn log — the child's own
  words, which are the whole diagnosis. No pid is claimed, so `status` still
  says NEVER_STARTED rather than STOPPED, because "it crashed" and "it never
  ran" are different things to tell someone. Every other test of the supervisor
  injects a fake spawner, which is exactly why nothing caught this; the new
  ones use a real subprocess.
- **A first-run remedy that pointed at the wrong document.** `start` without a
  configuration said to "copy the sample in docs/QUICKSTART.md". That page
  shows a TOML fragment and never names `config.toml` or
  `config.example.toml`, so an operator whose first command failed was sent
  somewhere that did not contain the thing they were told to copy. It names
  `configs/agent/config.example.toml` now, and the test asserts the file it
  names exists rather than asserting the wording.
- **The operator's loop is driven end to end.** `backup-save` → `prepare` →
  `run`, through the real CLI over a synthetic Zomboid directory. Every step had
  a unit test; the sequence did not, and the sequence is what a person performs.
  It is here for a specific reason: gating `run` on `prepare` creates the
  opposite risk to the one it closes, because a gate whose precondition can
  never be satisfied is a bricked release, and nothing could previously tell
  "refuses correctly" from "refuses always". The test asserts both directions.
  It also pins the three refusals an operator can actually hit — a save whose
  name does not say "test", a test save with no backup, and an evidence
  directory with no schemas.
- **A refusal that named no remedy now names one.** `prepare` reported
  "evidence schema missing" and stopped. The schemas ship in the archive's
  `evidence/schema/` and are in git in a checkout, so this is met only by
  pointing `--evidence-dir` somewhere new — or by running the bundled
  executable directly, where "the directory I came from" is a temporary unpack
  folder. Every other refusal in this project names its way out; this one did
  not. (The tempting fix — a second copy of the schemas inside the package —
  was started and reverted: it would have created a second source of truth for
  the documents that validate all release evidence, to improve a message.)
- **`live-test run` and `resume` refuse until `prepare` has completed.** They
  did not. `prepare` is the subcommand that proves the world is safe to
  experiment on — a save whose name marks it a test world, and a backup that
  *reads back* rather than merely existing — and it wrote `prepare.json` only
  when both held. Nothing read that file. So twenty scenarios that deliberately
  hurt the character and end in restores would start against any save at all,
  and the only thing between them and somebody's main world was a check whose
  answer went nowhere. `status` and `collect` stay ungated: reading the table
  and gathering logs change nothing, and gating them would leave an operator
  unable to see why they are stuck. The runner's own test fixture had never
  written a prepare record and every test passed, which is how this survived;
  the fixture writes one now and a second fixture exercises the refusal.
- **The eleven `.bat` wrappers are checked against the real parser.** They are
  the entire interface of the release — an operator installing from the ZIP
  never types `pz-agent` — and not one had ever been executed, here or on
  Windows. `tests/contract/test_bat_wrappers_invoke_the_real_cli.py` extracts
  every command line they build, expands the batch variables, and parses it.
  The risk is concrete: `--evidence-dir` belongs on the `live-test` group and
  not on its subcommands, so one transposed token would fail an operator's
  first command with an argparse usage message they could not act on.
- **The release archive carries the documents it tells you to read.** Fixing
  the "grep lists every guess" claim pointed five documents at
  `docs/GAME_API_VERIFICATION.md`, and `DOC_NAMES` did not ship it — so two
  shipped documents instructed an operator with no checkout to open a file that
  was not there. `docs/LOCAL_AGENT_PROMPT.md` was absent for the same reason,
  and it in turn told the agent to read `docs/PROGRESS.md`, also absent, as
  `docs/LIMITATIONS.md` did for `docs/RELEASE.md`. All four ship now.
  `tests/contract/test_release_docs_are_self_contained.py` follows every
  `docs/*.md` reference out of every shipped document and fails on a dangling
  one; contributor-only documents are exempt as a pinned literal set, so a new
  dangle fails rather than being waved through. One defect's fix created
  another within the hour, and only opening the archive showed it.
- **The blueprint's command names are accounted for.** `docs/blueprint/` is the
  requirement baseline and read-only, and it asks for two commands this build
  does not have under those names: `setup` (§14.2) and `support-bundle`
  (§14.7). Both were invisible to every test, because
  `tests/contract/test_cli_docs_agreement.py` globbed `docs/*.md` and never
  descended into the blueprint. That check now covers it, against a declared
  alias map, so a *third* unaccounted name fails rather than sitting there.
  Neither is a missing feature: the diagnostics bundle is `logs --bundle`, and
  the install flow is `install-mod` plus the separate steps QUICKSTART
  sequences. One part of §14.2 is a deliberate refusal rather than a
  simplification — the blueprint asks to back up an existing same-id mod before
  overwriting it, and `install-mod` audits first and **refuses**, naming the
  file, on anything it did not write or anything modified since it did. Backing
  up and overwriting would still have overwritten. Recorded with its reasoning
  in `docs/PROGRESS.md`.
- **The doctor's codes are documented.** `pz-agent doctor` stamps every check
  `PZD001`…`PZD010` and `README.md` bills `docs/TROUBLESHOOTING.md` as "Doctor
  codes and remedies"; `grep -rn 'PZD0' docs/` returned nothing, so the one
  instruction the tool gives a stuck user pointed at a page where their code did
  not appear. There is a table now, ordered as `doctor` runs the checks and
  saying which failures are consequences of an earlier one — and noting that
  `unknown` is not a pass. `tests/contract/test_doctor_codes_documented.py`
  pins it in both directions and checks each code against the check it belongs
  to, because a row naming the wrong check misdirects while passing a presence
  test.
- **`grep -rn "Build 42:" pz-mod/` is no longer described as the list of every
  guess.** It returns six lines in two files;
  `docs/GAME_API_VERIFICATION.md` marks 52 symbols `requires_live`. The claim
  appeared in five documents including `docs/LOCAL_AGENT_PROMPT.md`, where it
  read "Это исчерпывающий список" — so an agent working from that prompt would
  have enumerated six places and believed the unconfirmed surface covered. All
  five now point at the table and say what the grep is.
  `tests/contract/test_game_api_inventory.py` checks the table is complete
  against every engine class the mod constructs or probes for; its first
  version used a substring match and a mutation caught that, so it matches on a
  word boundary.
- **The mod names the capability that gates each action.** Five adapters —
  `equipment.equip`, `equipment.unequip`, `medical.bandage`, `survival.rest`
  and `survival.sleep` — declared `capability = nil`, each with a comment
  asserting that no probe existed for it. Probes exist for all five.
  `Toolkit.CAPABILITY` held six of the twelve names while its own comment
  claimed to spell them "exactly as `pz_agent_core.capabilities.probes` spells
  them", and the omission is what the five comments had read as absence. No
  command was ever ungated — the mod enforces by required symbols and the
  sidecar by the ledger — but the mod's published capability document named six
  capabilities where the system knows twelve, so five were missing from the
  report a person reads to find out why something was refused.
  `survival_sleep` is the one that matters most: its `experimental` ceiling
  exists because a sleeping character cannot be reached by a panic stop, and
  that ceiling was reaching nobody. `tests/contract/test_capability_declaration_agreement.py`
  compares both sides of the wire and was mutation-checked against a missing
  name and a wrong one.
- **Multiplayer is actually refused now.** It was documented as "refused in
  configuration and again at the session handshake", and neither refusal
  existed: a grep for "multiplayer" across `packages/` and `pz-mod/` found the
  warning's own text and two unrelated comments. `safety.allow_multiplayer`
  lived in `_advisories`, whose contract is "Never errors", carrying the
  sentence "multiplayer is refused at the handshake regardless of this setting"
  — so the flag loaded, the agent ran, and the only thing between it and a
  server was a line of advice describing a gate nobody had written. Now:
  the config key is a hard error; `observation.game.multiplayer` carries three
  states; and `ActionEngine._multiplayer_abort` refuses every mutating command
  unless the mod positively reported single player, with an **absent reading
  refused exactly as `true` is**. Stopping, disarming, cancelling and the three
  read-only actions stay exempt, because an agent that cannot be stopped in the
  one session it should not be running in is worse than no gate. Both halves
  mutation-checked. `isClient`/`isServer` are unconfirmed against Build 42.20
  like every other engine symbol, and are now the first row in
  `docs/GAME_API_VERIFICATION.md` for a reason: if they cannot be read, the
  agent refuses everything, which is correct and looks exactly like being
  broken.
- **`pz-agent smoke` is in `COMMANDS`.** It always had a parser, a dispatch
  branch and a working subsystem; it was missing from the tuple that declares
  what this build wires, so the CLI accepted a command its own list denied
  having. `tests/contract/test_cli_docs_agreement.py` treats `COMMANDS` as the
  truth about the surface, which made both of its directions wrong: a document
  naming `pz-agent smoke` failed for naming something "absent from the CLI",
  and the check that every real command is documented could never see it. The
  new `test_the_command_list_is_the_parser_and_the_parser_is_the_command_list`
  derives the set from the parser instead of restating it.
- **`scripts/generate_playbook.py`**, and a gate step that runs it with
  `--check`. `docs/LIVE_TEST_PLAYBOOK.md` said it was generated from
  `pz_agent_cli.livetest.scenarios` and had no generator and no check, so it
  could drift from the runner in silence. The generator reproduces the twenty
  existing scenarios byte for byte, which is what validates the template.
- **A real command executor in the mod.** `CommandReader` →
  `CommandDispatcher` → `ActionRuntime` → adapter, with an acknowledgement at
  every transition. One command in flight and one waiting, the lease re-checked
  before each step, TTL, idempotent replay, session validation, panic stop,
  manual takeover and heartbeat-loss stop. A success acknowledgement has one
  constructor and it requires observed evidence.
- **Seventeen Lua game adapters** covering movement, world and container
  inspection, inventory search/transfer/ensure-main, eating, drinking, reading,
  equipping, bandaging, resting and sleeping.
- **`tests/lua/test_adapter_registry.lua`**, which asks whether the adapters
  actually reach the dispatcher. They did not: thirteen of sixteen game actions
  were unreachable while every individual adapter test passed.
- **Python adapters** for the new actions, a deterministic medical triage
  policy, and capability probes for each.
- **`openai_compatible` and `teamon` plan providers**, over a standard-library
  HTTP transport with bounded retries, a response byte ceiling and separate
  connect and read timeouts. Credentials come from an environment variable named
  in config, never from the config file.
- **Handoff documentation** for a machine with the game installed:
  `docs/LOCAL_GAME_HANDOFF.md`, `docs/LIVE_TEST_PLAYBOOK.md`,
  `docs/LOCAL_DEBUG_MAP.md`, `docs/GAME_API_VERIFICATION.md` and
  `docs/LOCAL_AGENT_PROMPT.md`.

- **The whole MCP action surface.** Thirty-one tools, nineteen of them actions, so
  every action with a registered adapter can be asked for. A fourth tool kind,
  `QUERY`, covers the three that only read: they submit an action and return an
  action id like any other, and need no arming. `container.open_nearby` is
  refused entry to that kind by construction — its name reads like a query, but
  opening a container is a timed action the character performs.
- **Seam tests, as a category.** Every defect below was found by a test that
  crosses a boundary rather than covering a unit, because every subsystem
  involved was already written, tested and green on its own side:
  `tests/lua/test_adapter_registry.lua` (do the adapters reach the dispatcher),
  `tests/contract/test_adapter_args_agreement.py` (does the sidecar send what
  the mod declared), `tests/contract/test_capability_evidence_agreement.py`
  (can a capability ever be proven), `tests/contract/test_mcp_action_coverage.py`
  (is every action reachable, and does its tool publish arguments its adapter
  accepts) and `tests/contract/test_sidecar_capability_wiring.py` (does the
  assembled sidecar refuse everything).

### Fixed

- **The assembled sidecar refused every action.** `build_loop` never passed a
  capability check, so `SidecarLoop` kept its `deny_capability` default — which
  returns `False` for everything, by design, so that "nobody wired a probe"
  fails closed. All seventeen game adapters name a required capability, so a
  real session refused every one of them, always. No test saw it: each adapter
  and engine test injects its own check, and the production assembly path was
  the one thing none of them exercised.
- **A capability could never become `verified`.** `confirm()` is the only thing
  that promotes one, and nothing outside tests called it — in a build whose
  stated design is that only a live run promotes anything. Now wired, with the
  ack restated flat before `confirm()` sees it: the engine's `ActionResult`
  nests its evidence one level down and `missing_keys` matches at the top, so
  feeding it the engine result verbatim reported every key missing, silently.
- **`movement.move_near` could not be called at all.** It required a
  `RefKind.OBJECT` reference, and `PZAgent.ObserveModel` never mints one — a
  nearby thing that holds a container gets a `container:` reference and
  everything else gets a `square:` one. It refused every reference the mod is
  capable of producing.
- **Every movement command would have been refused.** The sidecar sent `target`
  as a nested object plus `square_ref` and four policy flags; the mod declares
  `x`, `y`, `z` and `radius`, because its dispatcher accepts only scalars. Six
  undeclared keys, and an undeclared key is a refusal. `inventory.transfer` and
  `inventory.ensure_main` had the same defect with an `origin` object, which no
  scalar declaration could ever have accepted; the origin is now read from the
  before-observation, which is a better source anyway — one is an assertion
  about the world, the other a reading of it.
- **The client-facing tool list went stale unnoticed.** `configs/mcp/README.md`
  advertised nineteen tools and seven actions. The test meant to catch that
  unions every document before comparing, so `docs/MCP_TOOLS.md` naming all of
  them satisfied it while the README fell arbitrarily far behind. It now asks
  per-document, and in both directions.
- **Adapters registered nowhere.** `Toolkit.declare` produced tables naming
  themselves under `name`, while `ActionRuntime` looks an adapter up by
  `adapter.action`. The mod would have loaded cleanly, reported healthy and
  answered `CAPABILITY_UNAVAILABLE` to every game action.
- **Adapter arguments were silently dropped.** `CommandDispatcher` builds the
  argument table from the adapter's declaration, so an adapter that declared no
  arguments ran with all of them gone rather than being refused. Declarations
  are now mandatory and asserted at load time.
- **`RUNTIME_OWNED` was referenced and never defined**, so `ActionRuntime.install`
  raised on any build where the adapters directory had published anything.
- **A lease expiring mid-flight was reported as `ACTION_TIMEOUT`**, which tells
  the sidecar its adapter is slow when in fact its own grant lapsed. It is now
  `LEASE_EXPIRED`; whether anything reached the character's queue is carried by
  the phase, which already distinguished `interrupted` from `rejected`.
- **`pz-agent restore-save` passed `game_running=False` unconditionally**, so it
  would have overwritten a save with the game open — the exact failure the
  keyword-only argument exists to prevent.

### Added

- Game-smoke harness (`pz-agent smoke`). A scenario that did not run is
  reported as not run — never as passing, never omitted — and a dry run cannot
  produce a pass, because it touched no game.
- `FINAL_IMPLEMENTATION_REPORT.md`, naming exactly what still requires a person
  with Project Zomboid installed.

### Added

- Repository foundation: package layout, `pyproject.toml`, ruff/mypy/pytest
  configuration, `.luacheckrc`, editor and git attributes.
- `pz_agent_core.version` as the single source of truth for the five versions,
  with a release gate that checks every place they are restated.
- Wire protocol package: closed enums, stable session-scoped references with
  generation tracking, and strict total parsers for commands, action results
  and observations.
- Safety invariant enforced in the type system: an `ActionResult` with status
  `succeeded` cannot be constructed without `POSTCONDITION_MET` and non-empty
  postcondition evidence.
- CI gate against forbidden shortcuts — stub bodies, `TODO` markers in shipped
  code, `eval`/`exec`/`shell=True`/`loadstring`, and committed secrets.
- GitHub Actions workflow covering Python 3.11/3.12, luacheck, Lua unit tests
  and a build artifact.
- Installation discovery across every Steam library, with an injectable
  filesystem root and environment so the Windows path is testable on Linux CI.
  Build detection reports an honest unknown rather than guessing.
- Save backup and restore with a hashed manifest. Restore refuses while the
  game is running and verifies every hash before writing; prune never removes
  the newest backup.
- File IPC: fixed layout, byte-offset journal reader that ignores a partial
  trailing line and skips a corrupt one, alternating-slot snapshots with the
  pointer written last, sequence gap detection, bounded idempotency cache and
  lease enforcement at both check points.
- Session handshake requiring a nonce different from the previous session, so a
  file left by a crashed sidecar cannot read as a fresh connection request.
- Lua mod for Build 42: pure shared modules (JSON with deterministic key order
  and no `loadstring`, references, protocol constants, sequences, queue
  ownership) and the engine-coupled client half, with a test harness that runs
  under a plain interpreter.
- Sixteen game-smoke scenario definitions, each naming the evidence that closes
  it.
- Documentation: protocol, architecture, safety, testing, compatibility,
  limitations, MCP boundary, quick start, troubleshooting, development and
  release.

- Capability model and read-only symbol scanner. A static scan yields
  `available_unverified` at best; only a live runtime confirmation produces
  `verified`, and a report from a different build downgrades every verified
  entry. The scan records symbol names, paths, signature lines and file hashes
  but never file contents.
- Action lifecycle engine. Preconditions are checked against an observation
  newer than anything already seen, and the mod's ack never overrides
  observation: without evidence from the adapter's verify, the result is
  `POSTCONDITION_FAILED` regardless of what the mod claimed.
- Deterministic selection policy for food, drink and literature, returning the
  score breakdown and the reason each rejected candidate lost.
- Observation diff, bounded store and the compact planner view, which is the
  only observation an LLM ever sees.
- Deterministic reflex guard, threat assessment and priority arbitration with
  anti-loop rate limiting. No LLM in the path, so it runs whether or not a
  planner is configured.
- Cross-language contract tests asserting the Lua and Python halves agree on
  versions, the action whitelist, reason codes, enums and IPC filenames.

- Typed planner, critic and executor. A plan structurally cannot carry code:
  `StepArgs` is a closed Protocol over a fixed parser table, so there is no
  field a Lua snippet, a shell string or a path could occupy. `NullProvider`
  plans deterministically from the policy modules, making `provider = "none"`
  a tested configuration rather than a claim.
- Sidecar attach/observe/act loop behind `start`, `stop`, `arm` and `disarm`.
  It attaches in OBSERVE, runs the reflex guard before anything else whether or
  not a planner is configured, and never re-arms itself after a restart.
- Windows installer and uninstaller that record a manifest of what they wrote
  and remove exactly that, so a file the user placed in the mod directory
  survives an uninstall.
- Doctor CLI, diagnostics with redaction applied as records are written, MCP
  boundary, permissions and autonomy engines, bounded save-scoped memory, and
  the voice companion.
- Lua observation producer, with a cross-language contract test that runs the
  builder under lua5.4, validates its output against the schema, parses it with
  the Python dataclasses and re-parses every reference.

### Fixed

- Every zombie in a horde shared one reference: the observer read `getOnlineID`
  first, which answers `-1` outside multiplayer, and `-1` was a legal reference
  segment. Threat assessment counts distinct references, so a horde read as one
  zombie.
- The inventory walk was unbounded on the game thread — nested bags multiplied
  to thousands of engine calls to produce a document that keeps 64 containers.
- Mutual exclusion did not hold: `O_EXCL` makes the lock file's *creation*
  exclusive, not the claim, so two sidecars could both report `acquired`.
- Backups were returned as complete without reading back what landed on disk,
  and restore hashed every file it copied and discarded the result.
- The support-bundle verifier reported the forbidden literal it found — in a
  report printed to a terminal and emitted as JSON, which would have been the
  leak it was reporting.

- `scripts/check.sh` ran luacheck but never executed the Lua tests, so failing
  assertions would not have been caught locally. It now runs them over the same
  glob CI uses.

[Unreleased]: https://github.com/natural0101/poject-zombigpt/compare/main...dev
