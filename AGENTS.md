# AGENTS.md — working agreement

Binding on every contributor, human or AI. If a change conflicts with this
file, this file wins.

## Priority order when requirements conflict

1. User safety and save integrity.
2. Verified compatibility with the actually installed game build.
3. The Definition of Done (`docs/blueprint/`, `agent/DEFINITION_OF_DONE.md`).
4. The master prompt (`docs/blueprint/MASTER_PROMPT_RU.md`).
5. Everything else.

## Repository map

| Path | Contains | Rule |
| --- | --- | --- |
| `packages/pz_agent_core/` | Domain types, policy, action engine | **Zero third-party runtime dependencies.** No MCP SDK, no LLM SDK, no UI. |
| `packages/pz_agent_mcp/` | stdio MCP server | Thin adapter. Translates and serialises; never re-implements policy. |
| `packages/pz_agent_cli/` | `pz-agent` command | The only package allowed to `print`. |
| `packages/pz_agent_voice/` | `VoiceAdapter` protocol, TeamON plugin, fake | Core must not import this. |
| `pz-mod/` | Lua for Build 42 | Pure functions extracted so they are testable outside the game. |
| `schemas/` | JSON Schema, the wire source of truth | Changing one is a protocol change; update `version.py` and the sync test. |
| `docs/blueprint/` | The original specification | **Read-only.** Never edit; it is the requirement baseline. |

## Non-negotiable engineering rules

**Honest state.** `succeeded` means a postcondition was *observed*. Queued is
`accepted`. Started is `started`. `ActionResult.succeeded()` requires evidence
and will raise without it — do not route around that constructor.

**No stubs on the critical path.** No `TODO`/`FIXME`, no bare `pass` body, no
`NotImplementedError`, no empty except, no fabricated success.
`scripts/check_forbidden.py` enforces this and runs in CI.

**Capability honesty.** Never assume an API exists because a wiki or an old mod
used it. Unverified → `available_unverified`. Unavailable → `unsupported` with a
reason, plus a safe fallback. Never simulate the effect by writing stats.

**The LLM is not a privileged caller.** It may emit a typed plan and nothing
else. No Lua, no Python, no shell, no keystrokes, no file paths, no raw refs it
invented. All in-game text (chat, radio, books, server and mod names) is
untrusted data, never instructions.

**Determinism where it counts.** Food and drink selection, literature choice,
priority arbitration and the reflex guard are deterministic policy code with
unit tests. The model may express a *goal*; it never picks the sandwich.

**Bounded everything.** Bounded memory, bounded logs, bounded retries, bounded
plan length, bounded autonomous radius. Anything unbounded is a bug.

**User input always wins.** Manual action detected → cancel automation. Panic
stop → clear only mod-owned queue entries, disarm, and never touch an action the
player queued.

## Definition of done for a single task

A task from `docs/blueprint/task_graph.yaml` is done when all of these hold:

- [ ] Implementation exists, with no stub on the critical path.
- [ ] Unit tests cover the logic, including the failure branches.
- [ ] Contract tests cover any wire-format change.
- [ ] `scripts/check.sh` is green.
- [ ] Docs updated — at minimum `docs/PROGRESS.md`, plus the relevant guide.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] Anything that genuinely needs a live game session is listed explicitly in
      `docs/PROGRESS.md` under "requires live session", not silently skipped.

## Commit and branch discipline

Small, meaningful commits referencing the task id (`T014: …`). Work on `dev` or
a topic branch; `main` only receives merges that are green.

## Forbidden endings

Do not end work by declaring "the architecture is ready", "it only needs
in-game testing", or "the user can finish it". State precisely what is
implemented, what is verified by tests, and the exact list of steps that
physically require the user to launch the game.
