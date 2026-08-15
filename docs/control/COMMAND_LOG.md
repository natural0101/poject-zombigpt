# Command log

Commands whose output is evidence for a step. Reproduce a step by running its
commands in order.

## Steps 1–10 (baseline)

```
git fetch --all --prune
git checkout dev && git pull --ff-only
git rev-parse HEAD                       # 873037c081800cf4f4373b9307fc1cdff3140e99
git checkout -b fix/windows-mcp-voice-runtime
git branch -a --format='%(refname:short) %(objectname)'
                                         # -> evidence/step-01-10/branches.txt
git stash -u && .venv/bin/python -m pytest tests/ && git stash pop
                                         # -> evidence/step-01-10/linux-baseline.txt
```

The Windows result is read from GitHub Actions rather than run here; there is no
Windows machine in this environment and a Linux run is not evidence about it:

```
# run 31156087690, head 873037c0, branch dev
#   -> evidence/step-01-10/windows-workflow-runs.txt
#   -> evidence/step-01-10/windows-failures.txt   (24 named tests)
```

## Progress accounting

The plan of record is `docs/control/MASTER_PLAN.yaml`, and these are its counter
and its gate:

```
.venv/bin/python scripts/master_report.py              # print
.venv/bin/python scripts/master_report.py --json       # the same figures, structured
.venv/bin/python scripts/check_master_plan.py          # refuse an unearned claim
.venv/bin/python scripts/audit_pass.py                 # refuse a claim history does not support
```

`audit_pass.py` needs the full history and exits 2 on a shallow clone; both
workflows check out with `fetch-depth: 0`. It is also a `check.sh` step, so it
runs without being remembered.

Nothing recounts and stores. `master_report.py` derives the percentage on every
read, so there is no stored number to drift; `scripts/reconcile_status.py` is
what writes `STATUS.json`, and it derives the same figures from the same plan.

The pair below counts `docs/control/PLAN.md`, the retired 100-step plan, and is
kept only because the steps 1–10 evidence was measured with it. Both now refuse
against the current `STATUS.json`, naming the plan it describes and the
successor above — before this they printed `PROGRESS: 0%` for a tree the same
file recorded at 73.31%, and `--write` stored that zero:

```
.venv/bin/python scripts/progress_report.py            # exits 1: wrong plan of record
.venv/bin/python scripts/check_progress.py             # exits 2: wrong plan of record
```
