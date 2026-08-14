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
| `docs/control/PLAN.md` | the 100 steps |
| `docs/control/STATUS.json` | the recorded state |
| `scripts/check_progress.py` | the gate |
| `scripts/progress_report.py` | the counter |

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

| Field | Value |
| --- | --- |
| archive | `pz-agent-windows-rc` (artifact 9225004153), `pz-agent-windows-v1.0.0-rc1.zip`, 77 entries |
| archive sha256 | `8be6712c6bce60f97b0f44e3c85d28348e67bba3d9619c1044b33ef7cb8276c1` |
| source commit | `f397d213758b63d69fa74295c05b5807b8629f18` |
| workflow run | https://github.com/natural0101/poject-zombigpt/actions/runs/31814473578 |
| certified by | `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 8 check(s) passed` — archive complete, all 11 wrappers at the root, **both executables in `bin/`**, 76 file digests matching, 8537 of 8586 tests passed with no failures, 31 MCP end-to-end testcases green, and the archive claiming no live-test evidence. The packaged pair also completed an MCP `initialize` over the RPC link with `PATH` reduced to the system directories |
| what this RC does *not* certify | the passed count has moved 8469 → 8476 → 8537 over the last three RCs as tests were added, and collected 8518 → 8525 → 8586; the sixty-one added here are the plan's `verify_command` fields, each put through the CLI parser. Skips have sat at 49 throughout, and the eight in that gap are the observation-seam round trip, which skips here for want of a Lua interpreter on the runner. That is not new and not a gap in this artefact: `test_adapter_args_agreement` — the command seam's equivalent, years older — skips on exactly the same condition, so **no seam check has ever been part of RC certification**. `test_capability_declaration_agreement` and `test_protocol_tables_agreement` join them on the same condition, which is why the skip count moves whenever a seam gains coverage. Both run on Linux CI and locally, both verify a contract between the mod and the sidecar that has nothing to do with the host OS, and putting Lua on the release runner would add a dependency to the release path for no coverage that is not already taken. Stated here so the growing skip count is read as what it is |
| current? | `docs/control/STATUS.json` → `release_candidate.status`; any code commit after the source commit makes it STALE until the workflow rebuilds it |
