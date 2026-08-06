# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

### Changed

- **Protocol 1.0 → 1.1.** The action whitelist grew from fifteen names to
  twenty-one, seventeen of them game actions. Added: `container.inspect`,
  `container.open_nearby`, `inventory.search`, `medical.bandage`,
  `survival.rest`, `survival.sleep`. `container.open_nearby` is deliberately not
  read-only — opening a container is a timed action the character performs, so
  placing it beside `world.inspect` would let an unarmed session move the
  character.
- **`inventory.equip` and `inventory.unequip` are now `equipment.equip` and
  `equipment.unequip`.** A rename, not an alias: the dispatcher's whitelist
  decides what may reach an adapter at all, and two spellings for one action is
  a second door. `SCHEMA_VERSION` stays at 1.0 — the document shapes did not
  change, only an enum inside them gained members.

### Added

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

- **The whole MCP action surface.** Thirty tools, eighteen of them actions, so
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
