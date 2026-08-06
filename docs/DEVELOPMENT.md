# Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
scripts/check.sh
```

Green here means green in CI — the script runs the same steps in the same
order. If they ever disagree, that is a bug in `check.sh`.

---

## Layout

```
packages/
  pz_agent_core/     domain types and business logic — ZERO runtime deps
  pz_agent_mcp/      stdio MCP server (thin adapter)
  pz_agent_cli/      the pz-agent command (the only place print() is allowed)
  pz_agent_voice/    VoiceAdapter protocol, TeamON plugin, fake for tests
pz-mod/42/media/lua/
  shared/PZAgent/    pure logic — loaded and tested without the game
  client/PZAgent/    engine-coupled
schemas/             JSON Schema — the wire source of truth
tests/               unit · contract · integration · lua · fixtures · game-smoke
scripts/             the check gate and its four project-specific checkers
docs/blueprint/      the original specification — READ ONLY
```

The dependency rule that matters: **`pz_agent_core` imports nothing outside the
standard library.** Not for elegance — it is what makes `provider = "none"` a
tested configuration rather than a claim. It also means core physically cannot
import the MCP SDK, so policy cannot drift into the MCP adapter.

`docs/blueprint/` is the requirement baseline. Never edit it. When the
implementation deviates, record the deviation in `docs/PROGRESS.md` with the
reason.

---

## The four project-specific gates

Ordinary linting catches ordinary mistakes. These catch the ones this project
cares about.

### `scripts/check_forbidden.py`

Walks the AST of every shipped file and fails on:

- a function body that is a bare `pass`, a `NotImplementedError`, a `...`
  outside a `typing.Protocol`, or nothing but a docstring;
- a `TODO` / `FIXME` / `XXX` / `HACK` marker in shipped code;
- `eval`, `exec`, `compile`, `os.system`, `os.popen`, `shell=True`,
  `pickle.load`, Lua `loadstring`;
- anything matching a secret pattern, in *any* tracked text file.

The stub rules exist because "I'll fill it in later" is how a critical path
acquires a silent no-op. The banned-primitive rules exist because the LLM
boundary must not be able to reach any of them — and a rule enforced by reviewer
attention is a rule that holds until the reviewer is tired.

Tests and `scripts/` are held to a looser standard: a test may legitimately
assert that `NotImplementedError` is raised, and the checker itself has to name
the functions it looks for.

### `scripts/check_versions.py`

Five versions move independently: product, protocol, schema, mod, supported
build. `pz_agent_core.version` is the source of truth; this fails when
`pyproject.toml`, `pz-mod/42/mod.info`, the schema `const`s or `CHANGELOG.md`
disagree with it.

A mod reporting a version the sidecar does not expect is a support ticket that
arrives looking like a crash.

### `scripts/check_schemas.py`

Asserts each document in `schemas/` still compiles as Draft 2020-12. A broken
`$ref` does not raise at validation time — it silently makes the validator
accept everything, which is worse than failing.

### `scripts/generate_playbook.py --check`

Regenerates the per-scenario half of `docs/LIVE_TEST_PLAYBOOK.md` from
`pz_agent_cli.livetest.scenarios` and fails when the file on disk differs. The
playbook said it was generated from that module and had no generator and no
check, so a scenario edited without regenerating shipped an operator a stale
procedure. Run it without `--check` to rewrite the file.

---

## Adding an action

Six places, and skipping any one of them produces a subtly broken action rather
than an obviously missing one.

1. **`schemas/command.schema.json`** — add the name to the `action` enum. The
   enum is closed; the mod rejects anything outside it before dispatch.
2. **`protocol/enums.py`** — add to `ActionName`. The contract test
   `test_every_action_name_is_in_the_schema_enum` fails if 1 and 2 disagree.
3. **A capability probe** in `capabilities/probes.py` — what must exist for this
   action to work, and how a runtime confirmation arrives.
4. **An adapter** in `actions/` implementing the `ActionAdapter` protocol. The
   part that takes thought is `verify`: *what observable change proves this
   happened?* If you cannot answer that, the action is not ready — there is no
   path to `succeeded` without evidence.
5. **The mod's dispatch table** plus the Lua side of the action.
6. **Tests** — unit for the adapter's verify logic, contract for the wire
   format, and a `tests/game-smoke/` scenario naming the evidence that closes it.

The `verify` question is the whole design in one method. "Did the command
return without error" is not a postcondition; "is the item now in the
destination container" is.

## Adding a capability

Never assume. Add the probe, run `pz-agent doctor` against a real install, and
let the report say what it found. A static scan yields `available_unverified` at
best — only a live runtime confirmation produces `verified`, and the code makes
that ordering structurally impossible to skip.

## Changing the protocol

1. Update the schema.
2. Update `protocol/` to match.
3. Update the Lua side — `shared/PZAgent/Protocol.lua` and `Refs.lua`.
4. Bump `PROTOCOL_VERSION` or `SCHEMA_VERSION` in `version.py` as appropriate.
5. Update the contract tests in both directions.

Reason codes are **append-only**. Renaming one is a protocol major bump,
because a client's recovery table is keyed by those strings.

---

## Style

Match the surrounding code. Concretely:

**Comments explain why, never what.** A comment restating the line below it is
noise. A comment explaining that references are parsed from both ends *because
a container tail contains colons and a naive split silently yields a valid
reference to a different object* is the reason the next person does not
"simplify" it.

**Every module has a docstring** saying what it is for and what invariant it
holds.

**Do not write comments addressed to a reviewer** or narrating the change —
"now with better error handling", "changed from the previous approach". The
diff says that; the file should read as if it had always been this way.

**Type everything.** mypy runs strict, including over tests.

**Frozen dataclasses** for domain types. Mutable shared state is where the
concurrency bugs in a polling system live.

**Inject the clock.** No test sleeps. If something needs time to pass, time is
a parameter.

## Testing

See [`TESTING.md`](TESTING.md). The short version: test the failure branches,
assert *what* rather than *that*, prove every documented bound with a test that
pushes past it, and remember that mocks prove logic and never prove engine
compatibility.

## Commits and branches

`main` is the released line, `dev` is integration, topic branches cut from
`dev`. Small commits, one concern each, referencing the task id from
`docs/blueprint/task_graph.yaml` when they close one (`T014: action lifecycle
framework`). Explain *why* in the body when the diff does not.

## Working with AI agents on this repository

[`AGENTS.md`](../AGENTS.md) is the working agreement and is binding on humans
too. When several agents work in parallel, give each a disjoint directory, tell
them not to touch the shared files (`CHANGELOG.md`, `docs/PROGRESS.md`,
`pyproject.toml`, `tests/conftest.py`, `tests/fixtures/__init__.py`), and have
them scope mypy to their own files — a repo-wide run mid-wave reports every
other agent's half-written module and nothing useful about their own.
