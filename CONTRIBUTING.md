# Contributing

## Branches

- `main` — released, tagged, always green.
- `dev` — integration branch; feature work merges here first.
- `claude/**`, `feat/**`, `fix/**` — working branches, cut from `dev`.

## Before you push

```bash
scripts/check.sh
```

That runs the same gate as CI: ruff format, ruff lint, mypy strict, the
forbidden-pattern scanner, the version-sync gate, schema validation and pytest.
A red local check will be a red CI run.

## The rules that are not negotiable

1. **No stubs on the critical path.** No `TODO`, no bare `pass` as a function
   body, no `NotImplementedError`, no `except:` without an exception type.
   `scripts/check_forbidden.py` fails the build on all four. A handler that
   *swallows* an exception is a review rule rather than a scanned one — see
   AGENTS.md for why it cannot honestly be scanned here — and it must carry a
   comment saying what is being discarded and why that is the smaller loss.
2. **Never report success you did not verify.** If an action was queued, say
   `accepted`. `succeeded` requires observed postcondition evidence, and the
   type system will not let you build one without it.
3. **If an API is unavailable, say so.** Report an honest capability state and a
   safe fallback. Do not simulate the outcome, and do not write the game's stats
   directly to paper over a missing timed action.
4. **Never touch a user's save without a backup.** The backup path is tested;
   use it.
5. **No secrets, ever.** Not in code, not in tests, not in fixtures.
6. **No game files in this repository.** No vanilla Lua, no assets, no saves.
   Compatibility metadata (names, signatures, hashes) is fine; source is not.

## Commits

Small and meaningful, one concern each. Explain *why* in the body when the
change is not obvious from the diff. Reference the task id from
[`docs/blueprint/task_graph.yaml`](docs/blueprint/task_graph.yaml) when a commit
closes one, e.g. `T014: action lifecycle framework`.

## Tests

New behaviour needs a test at the right level:

- `tests/unit/` — pure logic: scoring, policy, refs, parsing.
- `tests/contract/` — wire-format conformance against `schemas/`.
- `tests/integration/` — more than one subsystem, using the fake mod bridge.
- `tests/lua/` — Lua logic against the API mocks.
- `tests/game-smoke/` — scenarios that need a live session; documented, marked
  `game_smoke`, and skipped by default.

Mocks prove logic. They do not prove engine compatibility, and no comment,
docstring or report may claim otherwise.
