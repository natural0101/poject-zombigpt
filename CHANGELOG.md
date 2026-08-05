# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

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

### Fixed

- `scripts/check.sh` ran luacheck but never executed the Lua tests, so failing
  assertions would not have been caught locally. It now runs them over the same
  glob CI uses.

[Unreleased]: https://github.com/natural0101/poject-zombigpt/compare/main...dev
