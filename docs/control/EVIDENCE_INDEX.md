# Evidence index

Every path a step cites, with what it shows. `scripts/check_progress.py` refuses
a `PASS` whose evidence path does not exist, so this index and the tree agree by
construction.

## Steps 1–10 — baseline

| Path | What it shows |
| --- | --- |
| `docs/control/evidence/step-01-10/branches.txt` | every branch and its SHA at the start |
| `docs/control/evidence/step-01-10/windows-workflow-runs.txt` | the ten most recent Windows runs, all red |
| `docs/control/evidence/step-01-10/windows-failures.txt` | the 24 failing tests, named |
| `docs/control/evidence/step-01-10/linux-baseline.txt` | 3677 passed, 2 skipped at `873037c0` |
| `docs/control/PLAN.md` | the 100 steps, superseded by `MASTER_PLAN.yaml` |
| `docs/control/STATUS.json` | the recorded state |
| `scripts/check_progress.py` | the gate of the 100-step plan; refuses any other |
| `scripts/progress_report.py` | its counter; refuses any other |

The counter and gate of the plan of record are `scripts/master_report.py` and
`scripts/check_master_plan.py`, with `scripts/audit_pass.py` beside the gate for
the questions it cannot ask — whether a task's proof existed at the commit the
task names as its verification. That one is a `check.sh` step and is held by
`tests/unit/test_pass_audit.py`; before it was wired in, seven `PASS` claims
named a regression test added in a later commit than the one they recorded. The retired pair above is listed because these
steps were measured with it, not because it still reports anything: run against
the current `STATUS.json` both refuse by name. That refusal is the fix for a
counter that printed `PROGRESS: 0%`, `RC ARTIFACT: None` and `LIVE SCENARIOS:
0/20` for a tree the same file recorded at 73.31% with a fully identified
archive and 22 scenarios, and whose `--write` stored those zeroes.
`tests/unit/test_control_plane_reporters.py` runs all four and holds the printed
figures against the recorded ones.

## Steps 11–19 — evidence bytes are portable

| Path | What it shows |
| --- | --- |
| `docs/control/evidence/step-11-19/evidence-suites.txt` | the four affected suites, green after the fix |
| `docs/control/evidence/step-11-19/mutations.txt` | both mutations and the tests each turns red |
| `docs/control/evidence/step-11-19/linux-after.txt` | 3712 passed, 2 skipped after the fix |
| `tests/contract/test_evidence_bytes_are_portable.py` | 11 cross-platform digest tests |
| `tests/contract/test_windows_path_shapes.py` | Windows path shapes, asserted on Linux |
| `tests/contract/test_mcp_snippet_is_json.py` | the printed MCP block parsed back, 5 interpreter paths |

## The release candidate

The digest is the identity: an RC is *this* archive, from *this* commit, by
*this* run, and a claim about "the RC" that names none of the three is a claim
about nothing.

All three rows are held against `STATUS.json` by
`tests/unit/test_windows_workflow_contract.py` — this table is written by hand
beside a generated record, and for a long while only the digest was compared, so
the right sha256 could sit beside the wrong commit and the wrong run with the
suite green. The artefact id is the one field with nothing to check it against:
`STATUS.json` does not record one.

| Field | Value |
| --- | --- |
| archive | `pz-agent-windows-rc` (artifact 9249412139), `pz-agent-windows-v1.0.0-rc1.zip`, 77 entries |
| archive sha256 | `d0681086fbff7945b54ea8a85a1dd0dfb369771b69bfbc175c1cd7e24db7486d` |
| source commit | `9ab6faea72f1b931dfed24826566f9a2b8337fa6` |
| workflow run | https://github.com/natural0101/poject-zombigpt/actions/runs/31893420861 |
| certified by | `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 9 check(s) passed` — archive complete, the archive declaring the release this checkout builds, all 11 wrappers at the root, **both executables in `bin/`**, 76 file digests matching, 8841 of 8909 tests passed with no failures, 31 MCP end-to-end testcases green, and the archive claiming no live-test evidence. The packaged pair also completed an MCP `initialize` over the RPC link with `PATH` reduced to the system directories |
| what this RC does *not* certify | skips moved 49 → 63 → 68 and have held at 68 since. All nineteen added are seam checks that run the mod's Lua and find no interpreter on this runner — fourteen for the protocol tables at `1a5feb4`, five for the action-ack round trip at `8a803c2`. Everything added after them needs no interpreter and runs here: the mod-identity agreement at `e2b8978`, the evidence-manifest round trip at `0433a4b`. The nineteen join `test_adapter_args_agreement`, `test_capability_declaration_agreement` and the observation round trip, so **no seam check that needs Lua has ever been part of RC certification** — the count moves when such a seam gains coverage, never because something stopped working. All of them run on Linux CI and locally; each verifies a contract between the mod and the sidecar that has nothing to do with the host OS, and putting Lua on the release runner would add a dependency to the release path for coverage already taken elsewhere. Stated here so the skip count is read as what it is |
| current? | `docs/control/STATUS.json` → `release_candidate.status`; any code commit after the source commit makes it STALE until the workflow rebuilds it |
