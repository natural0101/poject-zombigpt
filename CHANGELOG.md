# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

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
