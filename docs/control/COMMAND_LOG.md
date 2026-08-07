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

```
.venv/bin/python scripts/progress_report.py            # print
.venv/bin/python scripts/progress_report.py --write    # recount and store
.venv/bin/python scripts/check_progress.py             # refuse an unearned claim
```
