# Decisions

Each entry is a choice that constrains later steps, with the reason. A decision
recorded after the fact is a rationalisation; these are written when taken.

## D-001 — the control system was created after the first code changes

Steps 14–19 and 41–45 were implemented **before** `docs/control/` existed,
during the previous instruction. The plan says the control files come first.
They did not, and this file says so rather than back-dating them.

Consequence: those steps are not credited as PASS on the strength of having been
done. They are re-verified against the same rules as everything else — a test
that fails without the fix, a full suite, a commit, an evidence path — and the
evidence recorded is from runs made *after* the control system existed.

## D-002 — the 24 Windows failures split into six root causes

From `docs/control/evidence/step-01-10/windows-failures.txt`:

| Cause | Tests | Steps |
| --- | --- | --- |
| Evidence digests taken from a string, written in text mode (CRLF) | 10 | 14–19 |
| Redactor left the native separator in the placeholder | 3 | 23 |
| Redactor rule order — the profile matched before the Zomboid directory | 1 | 24 |
| Installer: manifest separator, `len(body)` for size, launcher path | 3 | 28, 29 |
| MCP config assembled from f-strings, so a Windows path is not JSON | 2 | 41–42 |
| POSIX-only calls in tests (`signal.SIGKILL`, chmod-based unreadable) | 3 | 25–27 |
| Document-root globs comparing `docs\X.md` with `docs/X.md` | 2 | 22 |

## D-003 — a Windows claim needs a green Windows workflow, and only where it is a claim

`scripts/check_progress.py` requires `windows_ci.status == "GREEN"` for steps
30, 39, 40 and 91–95 — the ones that assert Windows *works*. It deliberately
does not require it for step 12, whose evidence is a red run, nor for steps
14–29, whose own evidence is a cross-platform test. A rule that is strict in the
wrong place is one that gets loosened wholesale the first time it blocks
something legitimate.

## D-004 — regression tests for Windows defects must fail on Linux

Every fix in stage 2 is covered by a test that constructs the Windows shape
explicitly (`PureWindowsPath`, bytes with `\r\n`, an injected writer that
translates newlines) so it fails here as well as there. A regression test that
only fires on the platform nobody develops on is one that fires after the
release.

## D-010 — the 100-step plan is retired; progress is weighted, not counted

The 100-step model reported `50%` for fifty passing steps. That number was a
count, and a count says a documentation paragraph and a live Project Zomboid
scenario are the same size. They are not, and the difference is the entire
question.

Replaced by `docs/control/MASTER_PLAN.yaml`: 480 tasks in 15 epics, each with a
weight in a validated band, and

    progress = sum(weight of PASS tasks) / sum(weight of all tasks) * 100

computed on every read. Nothing stores a percentage, so there is no number to
edit and none to drift.

**Weight bands are enforced.** `scripts/build_master_plan.py` refuses a plan
where a documentation task and a transport task, or a transport task and a live
one, could carry the same weight. Without that rule, weight becomes a habit
rather than a judgement, and the model decays back into a count.

**Nothing inherited a PASS.** `scripts/verify_carryover.py` ignores the old
record entirely. For each candidate it requires a named regression test, that
test existing, that test *passing when run*, an evidence path that exists, and a
commit that resolves. 193 tasks met all five. The gate then refused 11 of those
because they were PASS over an unfinished dependency; demoting them cascaded to
28. The honest figure fell from a claimed 50% to a measured **28.6%**.

**Seven metrics, because one number hides a zero.** MCP operability and voice
operability are both at 0.0% while Windows compatibility is at 89.3%. A single
figure would have averaged that into something that sounds like progress.

**An epic cannot close on task count.** Five conditions, all required, and the
last is an integration scenario that exercises the epic end to end. Every defect
this project has found was a subsystem that was complete, tested, green and
connected to nothing — which is exactly what a task count cannot see.
