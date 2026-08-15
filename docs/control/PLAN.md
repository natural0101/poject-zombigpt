# The 100-step plan

> **Superseded.** The plan of record is `docs/control/MASTER_PLAN.yaml` — 484
> weighted tasks rather than 100 equal steps — counted by
> `scripts/master_report.py` and gated by `scripts/check_master_plan.py`.
> `docs/control/STATUS.json` names it in `plan_of_record`, and the two scripts
> below refuse when that name is not this plan. This document is kept because
> the steps 1–10 evidence in `docs/control/evidence/` was measured against it.

One step is one percentage point. `overall_percent` is **counted by
`scripts/progress_report.py`**, never written by hand, and equals the number of
steps whose status is `PASS`.

Three separate totals, because they are separately blocked:

| Band | Steps | Ceiling | Who can do it |
| --- | --- | --- | --- |
| Remote implementation | 1–95 | 95% | this environment |
| Live game validation | 96–98 | 3% | an agent with Windows + Steam + Project Zomboid |
| Final release | 99–100 | 2% | after live validation |

"Done" is not a word that applies until step 100 is `PASS`. Until then the
honest form is `OVERALL: NN% · REMOTE: NN% · LIVE GAME: 0% · RELEASE: NOT READY`.

## What a PASS costs

A step is `PASS` only with **all** of:

1. a test that reproduces the defect and was seen to fail before the fix;
2. the fix;
3. that test passing after it;
4. the full related suite passing;
5. a commit SHA;
6. an evidence path recorded in `EVIDENCE_INDEX.md`.

`scripts/check_progress.py` refuses a `PASS` that has no evidence, no commit, a
gap behind it, an unsatisfied dependency, or — for a Windows step — a red
workflow run. It is a gate, not a report.

## What may never be counted

- code that is written and called by nothing;
- an interface with no concrete implementation;
- an MCP catalogue with no working transport;
- a voice intent that delivers no goal;
- a PyInstaller spec with no built executable;
- a ZIP whose contents were never checked;
- a workflow file with no successful run;
- documentation with no matching runtime;
- a Linux test result as evidence about Windows;
- a mock as evidence about Project Zomboid.

---

## Stage 1 — Baseline (1–10)

| # | Step | Owner |
| --- | --- | --- |
| 1 | Refresh local refs for every remote branch | remote |
| 2 | Switch to the current `dev` | remote |
| 3 | Create `fix/windows-mcp-voice-runtime` | remote |
| 4 | Record the starting commit SHA | remote |
| 5 | Record every branch and its SHA | remote |
| 6 | Record the recent CI workflow runs | remote |
| 7 | Record the current Linux test result | remote |
| 8 | Record the current Windows failures | remote |
| 9 | Create every `docs/control/` file | remote |
| 10 | First control commit | remote |

## Stage 2 — Windows foundation (11–30)

| # | Step | Owner |
| --- | --- | --- |
| 11 | Reproduce the full Linux suite | remote |
| 12 | Reproduce the full Windows suite through GitHub Actions | remote |
| 13 | Split the Windows failures by root cause | remote |
| 14 | Implement `canonical_json_bytes()` | remote |
| 15 | Move every evidence JSON writer to binary | remote |
| 16 | Atomic evidence writes through a temporary file | remote |
| 17 | SHA-256 over the actual bytes on disk | remote |
| 18 | `size_bytes` from the actual file size | remote |
| 19 | Cross-platform canonical JSON tests | remote |
| 20 | `portable_relative_path()` | remote |
| 21 | Manifest paths on the POSIX separator | remote |
| 22 | Document path checks corrected for Windows | remote |
| 23 | Redactor emits stable separators | remote |
| 24 | Redactor rule order: specific path before general | remote |
| 25 | Remove `os.geteuid()` from Windows-executable test paths | remote |
| 26 | Remove `signal.SIGKILL` from Windows-executable test paths | remote |
| 27 | Process tests hold a `subprocess.Popen` handle | remote |
| 28 | Existing config preserved byte for byte | remote |
| 29 | Windows installer and launcher path handling | remote |
| 30 | Zero Windows failures in this block | remote |

## Stage 3 — Windows installer (31–40)

| # | Step | Owner |
| --- | --- | --- |
| 31 | Quote every path in the BAT files | remote |
| 32 | Paths with spaces | remote |
| 33 | Paths with Cyrillic | remote |
| 34 | An existing `config.toml` is kept | remote |
| 35 | Installer manifest on Windows | remote |
| 36 | Uninstall with a user file inside the mod | remote |
| 37 | Uninstall does not remove a save | remote |
| 38 | Uninstall does not remove the config | remote |
| 39 | Synthetic installer round trip on Windows | remote |
| 40 | Commit: installer block green | remote |

## Stage 4 — MCP JSON (41–45)

| # | Step | Owner |
| --- | --- | --- |
| 41 | Build the MCP config as a dict | remote |
| 42 | Serialise only through `json.dumps` | remote |
| 43 | Test a Windows interpreter path | remote |
| 44 | Test Cyrillic and spaces | remote |
| 45 | Commit: MCP config correction | remote |

## Stage 5 — Local Core RPC (46–60)

| # | Step | Owner |
| --- | --- | --- |
| 46 | Document the Local Core RPC protocol | remote |
| 47 | JSON schemas for the request/response envelope | remote |
| 48 | Random RPC auth token | remote |
| 49 | Safe storage of the auth token | remote |
| 50 | Runtime descriptor `core-rpc.json` | remote |
| 51 | `AF_PIPE` transport for Windows | remote |
| 52 | `AF_UNIX` transport for Linux tests | remote |
| 53 | Forbid pickle serialisation | remote |
| 54 | Maximum request size | remote |
| 55 | Maximum response size | remote |
| 56 | Deadline and timeout | remote |
| 57 | Stale descriptor refused | remote |
| 58 | Protocol mismatch refused | remote |
| 59 | RPC server inside the sidecar | remote |
| 60 | Commit: Local Core RPC server | remote |

## Stage 6 — Remote core services (61–70)

| # | Step | Owner |
| --- | --- | --- |
| 61 | RPC client | remote |
| 62 | `RemoteCoreServices` | remote |
| 63 | Session operations | remote |
| 64 | Observation operations | remote |
| 65 | Action operations | remote |
| 66 | Plan operations | remote |
| 67 | Memory operations | remote |
| 68 | Diagnostics operations | remote |
| 69 | Capability operations | remote |
| 70 | Commit: `RemoteCoreServices` | remote |

## Stage 7 — MCP end to end (71–78)

| # | Step | Owner |
| --- | --- | --- |
| 71 | `--state-dir` on `pz-agent-mcp` | remote |
| 72 | Standard discovery of the RPC descriptor | remote |
| 73 | Wire `RemoteCoreServices` into `pz-agent-mcp` | remote |
| 74 | Launch a real MCP subprocess | remote |
| 75 | MCP `initialize` | remote |
| 76 | A real tool call | remote |
| 77 | A real resource read | remote |
| 78 | Commit: MCP E2E | remote |

## Stage 8 — Typed goal channel (79–84)

| # | Step | Owner |
| --- | --- | --- |
| 79 | Closed typed goal schema | remote |
| 80 | Bounded goal queue | remote |
| 81 | `goal.submit` | remote |
| 82 | `goal.status` | remote |
| 83 | `goal.cancel` | remote |
| 84 | Commit: typed goal channel | remote |

## Stage 9 — Voice (85–90)

| # | Step | Owner |
| --- | --- | --- |
| 85 | Working voice `PlanPort` over Core RPC | remote |
| 86 | Russian intents to `GoalKind` | remote |
| 87 | Keep the short stop path separate | remote |
| 88 | `TeamONBridgeClient` | remote |
| 89 | Fake subprocess bridge E2E | remote |
| 90 | Commit: voice E2E | remote |

## Stage 10 — Windows release (91–95)

| # | Step | Owner |
| --- | --- | --- |
| 91 | Build `pz-agent.exe` with PyInstaller | remote |
| 92 | Build `pz-agent-mcp.exe` with PyInstaller | remote |
| 93 | Both executables run on Windows | remote |
| 94 | Build `v1.0.0-rc1.zip` | remote |
| 95 | Windows package workflow green | remote |

## Stage 11 — Local game (96–98)

Performed by an agent with Windows, Steam and Project Zomboid. Nothing in this
environment can produce evidence for these, and no substitute counts.

| # | Step | Owner |
| --- | --- | --- |
| 96 | Live scenarios S01–S18 | local |
| 97 | Autonomous test, 30 minutes | local |
| 98 | Autonomous test, 2 hours | local |

## Stage 12 — Final release (99–100)

| # | Step | Owner |
| --- | --- | --- |
| 99 | Fix everything the live tests found | local |
| 100 | Release the confirmed v1.0.0 | local |
