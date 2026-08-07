# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

### Changed

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
