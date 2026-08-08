# Blockers

Built from the state of the repository, not from recollection. This file
previously said `Open: none` while CI was red and the plan held unearned
`PASS` claims; `scripts/check_master_plan.py` now refuses a plan whose
`BLOCKERS.md` says that while any task is `FAIL` or `BLOCKED`.

Three lists, because they gate different things. A live blocker does not stop
remote implementation; it stops `v1.0.0`. A remote blocker stops both.

Last reconciled at `62d69c3`, after an independent monitor found STATUS.json
carrying a stale HEAD, a percentage no calculation produced, a CI verdict
belonging to another commit, and `Open: none`. Re-reconciled at `276b9d9`,
the first commit on `main` where both workflows are green and the release
candidate was built from the commit they describe.

---

## REMOTE BLOCKERS

These stop the remote stage. Every one is reproducible in this environment.

### R-001 — `pz-agent-mcp` cannot serve: the MCP SDK 2.0 API break — **CLOSED**

Closed at `f7433ad`: `build_server` passes its handlers to the 2.0 constructor
(`on_list_tools`/`on_call_tool`/`on_list_resources`/`on_read_resource`) and
returns typed results; `require_sdk` inspects the constructor's signature and
refuses an SDK missing any of the four with `EXIT_SDK_INCOMPATIBLE` (9), so the
next API break is a diagnosis, not an `AttributeError`; `pyproject.toml` bounds
the extra at `mcp>=2,<3`; and the Linux workflow installs the `mcp` extra —
guarded by `tests/contract/test_ci_installs_what_the_tests_need.py`, which
failed the day the two workflows' extras diverged. The E07-M04 suite (28
subprocess E2E tests, 14 protocol tests) passes against a real child on both
platforms at `276b9d9`. The original record follows.

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

### R-002 — `run_stdio` has no diagnosable exit code for a build failure — **CLOSED**

An exception escaping `build_server` leaves the child exiting with Python's
generic 1 and a traceback, rather than one of the nine declared `EXIT_*` codes.
`test_no_two_refusals_share_a_code` cannot see this because the failure never
reaches the constant table. Even once R-001 is fixed, any SDK-shape mismatch
reaches a client author as a traceback instead of a diagnosis.

Closed: `EXIT_SERVER_FAILED` (10) — `main` wraps the serve call, and an
exception past every named refusal becomes one bounded stderr line naming the
exception's type and text, with stdout untouched because it belongs to the
protocol even in death. `KeyboardInterrupt` deliberately passes through: the
user's own hand is not a server failure. Both directions pinned in
`test_mcp_entry.py`; the code documented in `configs/mcp/README.md` (which
`test_mcp_exit_codes_documented.py` enforces) and `docs/TROUBLESHOOTING.md`.
With this, every remote blocker in this file is CLOSED.

### R-003 — the TeamON bridge exists twice, and the tested copy is not the shipped one — **CLOSED**

Closed: `pz_agent_voice/bridge/` is deleted. `teamon.py` is the only
implementation, it is what the tests import, and it is what the packaged
executable ships. The original record follows.

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
**CLOSED** — decided in `docs/control/DECISIONS.md`: `goal.submit`, because a
`PlanRequest` has nowhere to carry a quantity that is not free text, and
E09-M02-T003/T004 require one. Implemented; the companion submits a
`GoalRequest` and `plan.execute` is asserted absent from the wire.

### R-005 — `UnroutedPlanPort` still refuses goals in the CLI — **CLOSED**

E09-M01-T002 requires that no code path answers "not routed". The voice package
was clean; `pz_agent_cli/voice.py` (23 references) and `status.py` (1) were not.

Closed: `voice_services` calls `services_over_core_rpc`, `UnroutedPlanPort`,
`GoalUnroutable` and `GOALS_UNROUTED` are deleted, and a scan asserts no file
under `packages/pz_agent_cli/src` carries those names again — guarded by
requiring the scan to have found `voice.py` first, so a scan over nothing cannot
pass. `voice check` dials the channel rather than reading the descriptor,
because a sidecar killed rather than stopped leaves a descriptor naming a live
pid, and a file-only check answers "routed" with nothing listening.

The verifier found five claimed behaviours with no test that could catch their
absence, including that branch: a core that accepts the connection and then
refuses `goal.status` was reported ROUTED. All five are now covered.

### R-006 — the required workflows have not run against HEAD — **CLOSED**

CI and `windows package` for `62d69c3` were still in progress at reconciliation
time. Until both report against **this** commit, no CI-dependent task may be
`PASS`. A green run on a parent commit is not evidence about this one.

Closed at `276b9d9`: both workflows completed green against that commit
(CI run 31214158422, `windows package` run 31214158408), and
`scripts/check_master_plan.py` now enforces the rule structurally — STATUS.json
may only call a workflow GREEN for the commit it actually ran against, and the
staleness check refuses a STATUS whose named commit has code changes after it.

### R-007 — the voice intent resolver exists twice, and the tested copy is not the shipped one — **CLOSED**

Closed: `pz_agent_voice/intents.py` is deleted. `intent.py` survives because it
is what production runs — `session.py`, `config.py`, `phrases.py` and
`__init__.py` all import from it — and it now carries the properties the dead
copy had and it lacked: the percent sign as a spoken form of «процентов», a
closed `BARE_NUMBER_PARAM` table giving «прокачай механику до 7» its one honest
reading, an `IntentRefusal.INTERNAL` constant so a residual range-table drift
becomes a spoken sentence rather than a `ValueError` quoting the number the
user said, and import-time checks that every vocabulary word survives
normalisation, that no word — a stop word included — is claimed by two tables,
that every skill has a vocabulary, and that every refusal sentence fits the
speech bound. Dropped with the dead module, deliberately: its `too_long`
refusal (the survivor truncates at the transcript bound and matches the
surviving prefix — pinned behaviour), its `ambiguous_skill`/`unitless_number`/
`conflicting_parameters`/`missing_number`/`empty` codes (the survivor's folds
are pinned instead), its `NumericRange`-in-refusal field (the range is spoken
by `phrases.intent_refusal`), and its private idempotency-key mint and
`to_goal_request` (the session's `IdFactory` is the shipped route for the
channel's one caller-supplied string). `test_voice_intents.py` and
`test_voice_privacy.py` now name the survivor; both stop-first orderings — the
session's and the goal channel's — stay tested. The original record follows.

The R-003 shape, one layer up. Two modules map an utterance onto the goal
channel:

* `pz_agent_voice/intent.py` — what `session.py` imports and production runs
  (`classify`, `extract_quantities`, `resolve_goal`).
* `pz_agent_voice/intents.py` — 600+ lines whose docstring claims "the only
  place in the voice package that names a GoalKind", imported by **nothing in
  production**; only `test_voice_intents.py` and `test_voice_privacy.py` reach
  it.

Both implement the spoken-percent-to-fraction conversion independently
(`_SPOKEN_AS_PERCENT` in one, `_PERCENT_DIVISOR` in the other), which is how
the duplication was noticed. One of the two has to go, the survivor keeps the
better properties of each (the stop-first ordering and refusal vocabulary are
richer in `intents.py`; the wiring is `intent.py`'s), and the tests have to
name the survivor.
*Blocks:* closing E09-M02 honestly. Found at `743115c`; not yet resolved.

### R-008 — the shipped sidecar never serves the Core RPC router

**Severity: critical — the recurring defect, live.** `CoreRouter` (the server
half of the Core RPC link) exists only in `pz_agent_mcp/remote/server.py` and
is constructed by tests alone. `grep` over `packages/pz_agent_cli/src` finds
no import of it; `SidecarLoop` builds no `CoreServices` adapter over its real
session, observations, actions, planner, memory or goals; and the one caller
of `SidecarRpc.serve_rpc` outside its own module is a lifecycle test with an
echo handler. Every end-to-end test — the MCP subprocess E2E, the round trip,
the voice goal E2E — hosts the router *in the test process over fake ports*.

Consequence: `pz-agent start` launches a sidecar that publishes no descriptor
and serves no link, so a real `pz-agent-mcp` or `voice run` against a real
running sidecar finds nothing to connect to. The parts are tested and green;
nothing joins them. Found by the criterion-coverage audit
(`docs/control/evidence/criterion-audit-094cb8a.md`, E06-M04-T001).

*Closing needs:* a `CoreServices` adapter over the sidecar's real subsystems
in `pz_agent_cli`, the router served through `SidecarRpc` on start, and an
end-to-end test in which a second process reaches the *real* core — the mod
side faked at the exchange directory, nothing else.

### R-009 — twenty-two heavy claims rested on criteria their tests do not observe

The same audit refused 22 of the 75 weight-8+ PASS claims: each named test
passes without observing the stated criterion, so the criterion becoming
false would leave the suite green. The itemised gaps are in
`docs/control/evidence/criterion-audit-094cb8a.md`; among them: three of the
four waits on another process have no observed (or in two cases no existing)
deadline; the `recv_bytes` length bound is asserted nowhere; the eval/exec
rules of `check_forbidden` are never exercised against the shipped tree by
any test; several E12-M01 secret-hygiene claims scan doubles rather than the
real writers. All 22 tasks are back to IN_PROGRESS, with the ordering
cascade pulling 34 dependents with them; each returns to PASS when an
assertion observes its criterion.

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

### RB-001 — the release candidate is STALE — **CLOSED**

Built from `521f1e4` while HEAD was `62d69c3`. A ZIP that exists is not a
certification of the current tree. `scripts/check_master_plan.py` refuses a
STATUS.json marking an RC `CURRENT` whose `source_commit` is not HEAD.

Closed: the current release candidate was built from `276b9d90` by
run 31214158408 (`pz-agent-windows-rc`, archive sha256
`3eb5ca63c4d113f3a3a48914a7cd206f77439a24a80f891fcea60b205733bbe2`) and
STATUS.json records it `CURRENT`. The rule stands; any commit after the RC's
source commit makes it STALE again until the workflow rebuilds it.

### RB-002 — no `v1.0.0` may be tagged

Not while LIVE GAME VALIDATION is at zero. A version number is a claim about a
program that runs; nothing here has watched this one run.

### RB-003 — 0 of 54 integration checks pass — **SUPERSEDED: 48 of 54 pass**

An epic does not close on its task count. Every `CHECK` in the plan — the
statements about a milestone that no single task establishes — was open when
this was written. At `be28770` each runnable check's command was executed and
its outcome recorded in the plan: 48 pass. The six still open are E14's and
E15's — the live-game claims, which only a machine with the game can
establish. Thirteen of fifteen epics close under `epic_closed`'s five
conditions; E14 and E15 stay open, and with them RB-002.

---

## CLOSED WINDOWS DEFECTS — how each root cause reproduces

The six root causes of D-002 (`docs/control/DECISIONS.md`), each with the
command that reproduced it before its fix and guards against its return now.
Every command runs here on Linux as well as on `windows-latest`, per D-004:
each fix's regression test constructs the Windows shape explicitly, so the
command fails on any platform if the defect returns.

| Root cause | Reproduction command |
| --- | --- |
| Text-mode evidence digests (CRLF) | `.venv/bin/pytest tests/unit/test_livetest_evidence.py tests/unit/test_livetest_runner.py tests/unit/test_check_release.py -q` |
| Redactor left the native separator | `.venv/bin/pytest tests/unit/test_diagnostics_redaction.py tests/unit/test_diagnostics_bundle.py -q` |
| Redactor rule order (profile before Zomboid dir) | `.venv/bin/pytest tests/unit/test_diagnostics_redaction.py -q` |
| Installer: separator, `len(body)`, launcher path | `.venv/bin/pytest tests/unit/test_installer_windows.py -q` |
| MCP config from f-strings is not JSON on Windows | `.venv/bin/pytest tests/unit/test_mcp_configs.py tests/contract/test_mcp_snippet_is_json.py -q` |
| POSIX-only calls in tests (SIGKILL, chmod) | `.venv/bin/pytest tests/unit/test_cli_supervisor.py tests/unit/test_capabilities_scanner.py tests/unit/test_capabilities_report_io.py -q` |
| Document-root globs with mixed separators | `.venv/bin/pytest tests/contract/test_documented_commands_parse.py tests/contract/test_archive_documents_resolve.py -q` |

The original failing-test list is `docs/control/evidence/step-01-10/windows-failures.txt`.
