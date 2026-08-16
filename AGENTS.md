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

That rule binds what this code *claims*, not what it can *read*.
`ActionResult.from_dict` deliberately accepts a peer's `succeeded` carrying no
evidence, because the engine's job is to name that claim
(`POSTCONDITION_FAILED`, "the mod reported success but the postcondition was
never observed") and it cannot name what the decoder threw away. Moving the
check into `__post_init__` looks like hardening and is not: the ack is dropped
as unusable and the answer degrades to `ACTION_TIMEOUT`, which says the mod went
quiet when in fact it lied. `tests/unit/test_a_success_claim_without_evidence_is_named.py`
holds that line.

**No stubs on the critical path.** No `TODO`/`FIXME`, no bare `pass` body, no
`NotImplementedError`, no `except:` without an exception type, no fabricated
success. `scripts/check_forbidden.py` enforces those and runs in CI.

**A handler that swallows is a review question, not a scanner one**, and this
paragraph used to claim otherwise: it listed an empty exception handler among
the things CI rejects, and no such check existed. It cannot be one honestly. The tree
contains `except (OSError, UnicodeDecodeError): pass` that falls through to a
second lookup, and several `except OSError: return` that deliberately trade a
diagnostic for a session in flight, each with the reasoning written above it.
A scanner cannot tell those from a swallowed failure, so the rule is: **a
handler that discards an exception must say in a comment what it is discarding
and why that is the smaller loss.** Reviewers enforce it. What the scanner does
enforce is the untyped `except:`, which is never right here because it also
catches `KeyboardInterrupt` and `SystemExit`.

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

**A code commit is followed by a STATUS commit, and `check.sh` is run after the
code commit rather than before it.** `docs/control/STATUS.json` records a CI
verdict *together with the commit it belongs to*, because a workflow result for
one commit is no evidence about another. Committing code therefore invalidates
it, and `check_master_plan.py` — which runs inside `scripts/check.sh` — refuses
a plan whose STATUS claims a bare `GREEN` for a commit that is no longer `HEAD`.
Run the gate before the commit and it passes on a tree that no longer exists;
that is how a green local check ships a red CI.

The sequence:

1. Commit the code.
2. `.venv/bin/python scripts/reconcile_status.py` with the verdicts *observed*
   for the previous commit and the SHA each belongs to. Nothing here is typed
   from memory, and a status with no SHA cannot be recorded at all.
3. `bash scripts/check.sh` — now against the tree that will be pushed.
4. Commit STATUS **alone**. The gate allows a later verdict-recording commit
   only when nothing outside `docs/control/` changed, so anything else in that
   commit turns the allowance off.
5. Push — **both commits, never the code commit alone.** The two are one unit.
   A code commit cannot carry a STATUS describing itself (writing the file
   changes the tree, which changes the SHA), so *every* code commit here is
   inadmissible to its own gate: measured, 12 of the last 25 commits on `dev`
   fail `check_master_plan.py` against their own tree, and they are exactly the
   code ones. That is harmless while the pair travels together and permanent
   the moment it does not — pushing the code commit alone on 2026-08-16 put a
   red CI run on `c4b08ef` for a tree that was never meant to be judged.

Step 3 is not optional and its order is not cosmetic. Run the gate *before* the
code commit and it reports on a tree that will never be a commit — which is why
it now says which tree it judged, first and last, instead of a bare "All checks
passed" (`scripts/check_tree_identity.py`). A green line with no subject is what
the 2026-08-16 push was read off.

Pass the RC's identity (`--rc-sha`, `--rc-run`, `--rc-sha256`) on every
reconcile that keeps an archive, `STALE` ones included. `STALE` describes the
archive's relation to the tree; it does not retract its name. Omit the flags and
the fields go null, which is not a modest claim but an empty one — an RC is
*this* archive, from *this* commit, by *this* run.

## Forbidden endings

Do not end work by declaring "the architecture is ready", "it only needs
in-game testing", or "the user can finish it". State precisely what is
implemented, what is verified by tests, and the exact list of steps that
physically require the user to launch the game.
