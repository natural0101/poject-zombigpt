# pz-agent

A local AI agent for **Project Zomboid Build 42.20 Stable** on Windows 10/11.

It is not a macro recorder and not a screen-scraping bot. The agent reads
structured game state through an in-game Lua mod, performs **verified** game
actions through the engine's own timed-action system, and exposes that surface
to an LLM over MCP — behind a policy layer the LLM cannot talk past.

> **Status:** under active construction. See [`docs/PROGRESS.md`](docs/PROGRESS.md)
> for exactly which parts are implemented, which are stubs, and which require a
> live game session to verify. Nothing in this README describes behaviour that
> is not backed by code and tests.

---

## What makes it different

**Success means the world changed.** Every action is a transaction:
`validate → prepare → enqueue → observe → verify → finalize`. An action reports
`succeeded` only when a *postcondition was observed* — hunger actually dropped,
the item is actually in the destination container. Queuing something is never
success. This is enforced in the type system: `ActionResult.succeeded()` refuses
to build a result without postcondition evidence.

**The LLM plans; it does not act.** The planner returns a typed plan validated
against a JSON Schema. It cannot emit Lua, Python, shell, keystrokes, or file
paths. The executor takes one step at a time and re-observes between steps.

**Safety is deterministic, not learned.** A reflex guard with no LLM in the loop
handles panic stop, manual takeover, threat interruption and heartbeat loss. It
runs whether or not a planner is configured.

**Capabilities are probed, not assumed.** The mod reports what it *verified* at
runtime against the actual installed build. An unverified API is never published
as a ready MCP tool — it is reported honestly as `unsupported` with a reason.

---

## Safety model in one screen

| Guarantee | How it is enforced |
| --- | --- |
| Mod is off by default | `session.arm` is required before any mutating command is accepted |
| Panic stop always works | `safety.stop` bypasses the queue and the arming check |
| Player input wins | Any manual action sets `manual_takeover`; automation cancels itself |
| No stale commands | Every command carries a lease (TTL); expiry is re-checked before execution |
| No duplicate execution | Terminal results are cached per idempotency key and replayed |
| Save is protected | A backup is required before the first autonomous run |
| No arbitrary execution | `eval`/`exec`/`shell=True`/`loadstring` are banned by a CI gate |
| Untrusted text stays untrusted | Chat, radio, book, server and mod names are never treated as instructions |

Full model: [`docs/SAFETY.md`](docs/SAFETY.md) · [`SECURITY.md`](SECURITY.md) ·
[`PRIVACY.md`](PRIVACY.md)

---

## Architecture

```
Project Zomboid (Kahlua)
  └── pz-mod  ── heartbeat, observation, timed-action adapters, panic stop
        │
        │  file IPC in ~/Zomboid/Lua/pz_agent/  (JSONL journal + atomic snapshots)
        ▼
  pz_agent_core  ── protocol · session · ipc · observation · actions
                    policy · planner · memory · safety · diagnostics
        │
        ├── pz_agent_mcp    stdio MCP server (thin adapter, no duplicated policy)
        ├── pz_agent_cli    pz-agent doctor / install-mod / start / backup-save / …
        └── pz_agent_voice  VoiceAdapter protocol + TeamON plugin + fake for tests
```

Why file IPC: sockets from inside a Kahlua mod are restricted, while writes to
the user's Lua directory are supported and observable. The protocol is a
journal of commands and acknowledgements with independent monotonic sequences,
not a shared mutable blob — see [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## Quick start

Requirements: Windows 10/11 x64, Steam Project Zomboid Build 42.20, Python 3.11+.

```powershell
git clone https://github.com/natural0101/poject-zombigpt
cd poject-zombigpt

py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

.venv\Scripts\pz-agent doctor         # verify install, paths, build, permissions
.venv\Scripts\pz-agent backup-save    # required before any autonomous run
.venv\Scripts\pz-agent install-mod    # copy the bridge into ~/Zomboid/mods/
```

Then launch Project Zomboid, enable **PZ Agent Bridge** in the mod list, load
your save, and:

```powershell
.venv\Scripts\pz-agent play           # start, wait for the game, arm assisted
.venv\Scripts\pz-agent status --watch # live view: game, session, goal queue
.venv\Scripts\pz-agent goal status    # what the agent is working on
```

`play` is the one-command path: it starts the sidecar, waits for the game to
connect, and arms ASSISTED — every step confirmed by what the game reported,
every wait bounded. The explicit route is still there and is the better one to
walk first: `pz-agent start` attaches in OBSERVE and does nothing at all until
`pz-agent arm`. Full walkthrough: [`docs/QUICKSTART.md`](docs/QUICKSTART.md);
the play loop end to end: [`docs/PLAYING.md`](docs/PLAYING.md).

---

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
scripts/check.sh          # ruff + mypy + forbidden-pattern gate + schemas + pytest
scripts/check.sh fast     # skip integration tests
```

Branches: `main` is the released line, `dev` is integration. See
[`CONTRIBUTING.md`](https://github.com/natural0101/poject-zombigpt/blob/main/CONTRIBUTING.md) and [`AGENTS.md`](https://github.com/natural0101/poject-zombigpt/blob/main/AGENTS.md) — the latter is
the working agreement for AI agents contributing to this repository, and it is
binding on humans too.

---

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Install, arm, first verified action |
| [`docs/PLAYING.md`](docs/PLAYING.md) | The play loop: one command, watch, goals |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Package boundaries and data flow |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | IPC files, sequences, refs, recovery |
| [`docs/MCP_TOOLS.md`](docs/MCP_TOOLS.md) | Every tool and resource, with schemas |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Modes, risk classes, reflex guard |
| [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) | Verified API journal per build |
| [`docs/TESTING.md`](https://github.com/natural0101/poject-zombigpt/blob/main/docs/TESTING.md) | Unit, contract, Lua harness, game smoke |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Doctor codes and remedies |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | What this cannot do, and why |
| [`docs/DEVELOPMENT.md`](https://github.com/natural0101/poject-zombigpt/blob/main/docs/DEVELOPMENT.md) | Layout, the four CI gates, adding an action |
| [`docs/RELEASE.md`](docs/RELEASE.md) | Version rules and the release gate |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | Live task-graph status |
| [`docs/blueprint/`](https://github.com/natural0101/poject-zombigpt/blob/main/docs/blueprint/) | The original specification this implements |

---

## Scope and limits

Single-player only. The agent refuses to operate against multiplayer servers —
automating another operator's server is out of scope: the configuration key is
refused outright, and the action engine will not issue a mutating command unless
the mod positively reports single player.
Combat exists on exactly one rung: ASSISTED, single-target, behind the
`combat_assist` capability and an explicit user-submitted goal or tool call.
One bounded attack window per command — a handful of swings, terminal when the
window closes — with a deterministic policy refusing groups over the limit,
critical endurance or panic, heavy injury and a broken weapon, and success
claimed only from the re-observed zombie, never from the swing. The capability
is `experimental` until a live session confirms the attack entry points, so on
a clean install the combat tools are withheld rather than offered. Autonomous
combat remains unsupported by design — the `autonomous_attack` probe keeps its
hard ceiling and no initiative path, arbiter or planner ever proposes a combat
action — until (and unless) its own live-gated epic argues otherwise, and the
reflex guard still stops everything at CRITICAL danger.
It does not drive, and does not alter game statistics directly
to paper over a missing API. Where an API cannot be verified, the capability is
reported as unsupported rather than faked.

## License

MIT — see [`LICENSE`](LICENSE). Project Zomboid is a trademark of The Indie
Stone; this project is unaffiliated and ships no game code or assets.
