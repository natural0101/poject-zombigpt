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

## The voice companion routes through `goal.submit`, not `plan.execute`

Decided at `ed35e81`. An agent building the voice plan port raised this rather
than guessing, which was the right call: it had wired `PlanPort` because that is
what `VoiceSession._start_goal` calls, and asked whether the typed goal channel
was meant to replace it before either milestone was marked done.

**The answer is the goal channel, and the reason is parameters.**

The privacy argument, which is the one that looks decisive, turns out not to be.
`VoiceGoal` is already a closed `StrEnum` of four tokens — `eat`, `drink`,
`read`, `resume` — and `PlanRequest.goal` receives `goal.value`, so no
transcript text crosses that boundary today. `PlanRequest.goal` being typed
`str` is a latitude nothing exercises. Routing voice through `plan.execute`
would not leak a transcript.

What it cannot do is carry a *quantity*. `VoiceGoal` has no parameters at all,
and E09-M02 requires two things of the intent layer that depend on having them:
T003, that quantities and targets become typed fields, and T004, that a
parameter outside its declared range is refused. "Прочитай двенадцать страниц"
has to arrive somewhere as `pages=12`, and be refused at `pages=999`. There is
nowhere in a `PlanRequest` to put it that is not a substring of the goal string
— which *would* be transcript text, and would be the leak the privacy floor
forbids. So the two milestones are not independent: satisfying E09-M02 through
`plan.execute` requires reintroducing free text.

`GoalKind` carries exactly what is needed, and three of the four voice tokens
map onto it without invention:

| `VoiceGoal` | `GoalKind` | parameters |
| --- | --- | --- |
| `eat` | `satisfy_hunger` | `satisfy_to` (optional) |
| `drink` | `satisfy_thirst` | `satisfy_to` (optional) |
| `read` | `read_for_boredom` | `pages` (optional) |
| `resume` | — | not a goal; a control verb over the active one |

`resume` is the interesting one and it is *not* a defect in the mapping. It does
not name work to do, it names something to do to work already submitted, which
is what `goal.status` and the queue's own activation answer. Mapping it onto a
`GoalKind` would have invented a fifth kind that the planner has no way to
serve — a stub wearing an enum member's clothes, which `goals/model.py` says in
as many words it will not have.

Three things come with the channel that `plan.execute` does not have, and each
is a requirement somewhere in E08 or E09 rather than a nicety: one active goal
at a time with the refusal naming the active one, a bounded budget in wall clock
*and* steps so a spoken goal cannot run forever, and an idempotency key so a
retried utterance is not a second sandwich.

`train_skill` and `learn_recipe` have no voice phrasing yet. They are reachable
from MCP and not from a microphone, and that asymmetry is recorded here rather
than closed, because inventing Russian phrasings for them is a grammar decision
and not a wiring one.

## D-012 — the decisions that shaped the release candidate

The RC named in `docs/control/EVIDENCE_INDEX.md` is the shape it is because of
these, each recorded where it happened and gathered here so an operator does
not have to read the git log to know why the archive looks the way it does:

* **Two executables, both required to answer.** `pz-agent.exe` (sidecar + CLI)
  and `pz-agent-mcp.exe` (the MCP client entry) are built separately, and the
  workflow's "Both executables answer" step runs each one — building is not the
  claim, answering is. A build that produces a binary that dies on startup
  fails the workflow, not the user.
* **The MCP SDK is bound, and its absence is a diagnosis.** `mcp>=2,<3` after
  the 2.0 API break (R-001); the entry point exits with a distinct code for "no
  SDK" (3) and "an SDK whose constructor no longer takes the four handlers" (9),
  because a client author reading a traceback is the failure mode this whole
  exit-code table exists to prevent.
* **PyInstaller discovers the SDK by directory, not by import.** `mcp.cli`
  calls `sys.exit` at import time without its optional `typer` extra, and
  `SystemExit` is not an `Exception`, so import-driven discovery died inside
  PyInstaller's `on_error` hook. `packaging/windows/specutil.py` reads the
  package directory instead. Recorded in the step-30-40 evidence file at the
  commit that fixed it (`15296e50`).
* **The archive is verified member-by-member.** Digests are taken over each
  member's bytes on disk, independently of the index that names them, and the
  index itself is bounded before parsing (a bracket-count depth scan, not a
  `RecursionError` catch, because the latter is a property of the interpreter,
  not the input). A member name is classified by what is wrong with it —
  absolute, drive-letter, traversal — and refused without quoting it.
* **The release gate runs in the same workflow that built.** `check_release.py`
  refuses an archive whose manifest, digests or required documents do not hold,
  so an RC artifact existing at all means the gate passed on the runner that
  made it.
* **No `v1.0.0` from any of this.** The RC is a certified build, not a
  release; LIVE GAME VALIDATION is 599 of 3104 weight and owned `local`, and
  RB-002 stands until scenarios run on a machine with the game.
