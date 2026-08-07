# Blockers

Built from the state of the repository, not from recollection. This file
previously said `Open: none` while CI was red and the plan held unearned
`PASS` claims; `scripts/check_master_plan.py` now refuses a plan whose
`BLOCKERS.md` says that while any task is `FAIL` or `BLOCKED`.

Three lists, because they gate different things. A live blocker does not stop
remote implementation; it stops `v1.0.0`. A remote blocker stops both.

Last reconciled at `62d69c3`, after an independent monitor found STATUS.json
carrying a stale HEAD, a percentage no calculation produced, a CI verdict
belonging to another commit, and `Open: none`.

---

## REMOTE BLOCKERS

These stop the remote stage. Every one is reproducible in this environment.

### R-001 — `pz-agent-mcp` cannot serve: the MCP SDK 2.0 API break

**Severity: critical. This is the largest single finding of the reconciliation.**

`packages/pz_agent_mcp/src/pz_agent_mcp/server.py:121-124` registers handlers
with the SDK 1.x decorator-factory API:

```python
server.list_tools()(list_tools)
server.call_tool()(call_tool)
server.list_resources()(list_resources)
server.read_resource()(read_resource)
```

`pyproject.toml` declares `mcp = ["mcp>=1.2"]` with **no upper bound**, and the
resolved install is **mcp 2.0.0**, where `mcp.server.lowlevel.Server` has none
of those methods. Confirmed directly:

```
>>> from mcp.server import lowlevel
>>> s = lowlevel.Server("x"); hasattr(s, "list_tools")
False
```

The child dies with `AttributeError` before answering `initialize`. This is not
a rename: a 2.0 handler is `async (ServerRequestContext, params) -> Result` and
must return `types.ListToolsResult` / `types.CallToolResult` / etc., while the
four functions in `build_server` return bare `list[Any]` / `str`.

Why it went unnoticed for so long, and why it matters more than its size:
**`--describe` still exits 0 with the full catalogue**, and every test that does
not open a protocol connection still passes. This is exactly the substitution
the plan forbids — treating `--describe` as evidence of a working server — and
`test_describe_succeeding_does_not_imply_a_working_transport` is now catching it
for real rather than hypothetically.

*Blocks:* all of E07 (MCP OPERABILITY is 0.0% and honestly so), E11-M02-T005.
*Closed by:* an SDK-2.0 registration path, a version bound in `pyproject.toml`,
and the E07-M04 suite passing against a real subprocess.

### R-002 — `run_stdio` has no diagnosable exit code for a build failure

An exception escaping `build_server` leaves the child exiting with Python's
generic 1 and a traceback, rather than one of the nine declared `EXIT_*` codes.
`test_no_two_refusals_share_a_code` cannot see this because the failure never
reaches the constant table. Even once R-001 is fixed, any SDK-shape mismatch
reaches a client author as a traceback instead of a diagnosis.

### R-003 — the TeamON bridge exists twice, and the tested copy is not the shipped one

Two modules implement the same thing:

* `packages/pz_agent_voice/src/pz_agent_voice/bridge/` — 1881 lines, **zero
  tests**, imported by nothing. `grep` finds only a docstring cross-reference.
* `packages/pz_agent_voice/src/pz_agent_voice/teamon.py` — what
  `tests/unit/test_teamon_bridge.py` and `tests/contract/test_teamon_bridge_e2e.py`
  actually import.

Every E10 claim therefore rests on a module that may not be the one shipped, and
1881 lines of production code have never been executed. One of the two has to
go, and the tests have to name the survivor.
*Blocks:* all of E10, and VOICE OPERABILITY beyond the intent layer.

### R-004 — the voice companion targets `plan.execute`, not the typed goal channel

`pz_agent_voice/plan_port.py` routes a spoken goal through `plan.execute`, while
E09-M02 maps intents onto `GoalKind` and the channel is reached by
`goal.submit`. Both exist; nothing decides which one voice uses. Recorded rather
than guessed at.
*Blocks:* E09-M01 and E09-M02 closing together.

### R-005 — `UnroutedPlanPort` still refuses goals in the CLI

E09-M01-T002 requires that no code path answers "not routed". The voice package
is clean; `pz_agent_cli/src/pz_agent_cli/voice.py` (23 references) and
`status.py` (1) are not, and two test files assert the old behaviour.

### R-006 — the required workflows have not run against HEAD

CI and `windows package` for `62d69c3` were still in progress at reconciliation
time. Until both report against **this** commit, no CI-dependent task may be
`PASS`. A green run on a parent commit is not evidence about this one.

---

## LIVE BLOCKERS

These cannot be closed from this environment at all. Nothing here has a Project
Zomboid installation, and `check_master_plan.py` refuses a `local`-owned task
marked `PASS` from here.

### L-001 — no scenario has ever run against the game

`LIVE GAME VALIDATION` is 0.0% of 599 weight — a fifth of the whole plan.
20 scenarios, 0 passed, 0 failed, 20 not run.

### L-002 — no capability has been verified against a running build

Every capability is `available_unverified` or `unsupported` by scan. Whether
the Build 42.20 API actually behaves as the adapters assume is unknown.

### L-003 — the executables have never been run on a machine with the game

They are built and answer `--version` / `--describe` on a CI runner. That is
not the same as running beside Project Zomboid.

---

## RELEASE BLOCKERS

Everything above, plus:

### RB-001 — the release candidate is STALE

Built from `521f1e4`; HEAD is `62d69c3`. A ZIP that exists is not a
certification of the current tree. `scripts/check_master_plan.py` refuses a
STATUS.json marking an RC `CURRENT` whose `source_commit` is not HEAD.

* workflow run: https://github.com/natural0101/poject-zombigpt/actions/runs/31190105731
* artifact: `pz-agent-windows-rc`, id 8998557228
* archive sha256: `0313ea8ffbfdef0dd7773ff25802af4caf09dc88a2f912db8cfa602c415d7c03`
* source commit: `521f1e45a38887e2bc2f88203ece238afd914bc1`

### RB-002 — no `v1.0.0` may be tagged

Not while LIVE GAME VALIDATION is at zero. A version number is a claim about a
program that runs; nothing here has watched this one run.

### RB-003 — 0 of 54 integration checks pass

An epic does not close on its task count. Every `CHECK` in the plan — the
statements about a milestone that no single task establishes — is open.
