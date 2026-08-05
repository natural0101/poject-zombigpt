# Game smoke scenarios

Fifteen scenarios that **cannot** be closed without a running Project Zomboid
session. Everything else in this repository is verified by automated tests; this
directory is the honest accounting of what is not.

Each scenario is a YAML file with preconditions, steps, and — the part that
matters — the **evidence** that closes it. A scenario is not "done because it
seemed to work". It is done when the named artefact exists and says what it is
supposed to say.

## Running

```powershell
.venv\Scripts\pz-agent smoke --scenario S05 --evidence-dir evidence/
.venv\Scripts\python -m pytest -m game_smoke --run-game-smoke
```

The harness automates every step it can: it drives the sidecar, captures the
observation stream, and extracts the evidence fields from the action results.
What it cannot automate is marked `manual: true` in the scenario — pressing a
key, walking into a zombie, closing the game. Those steps print an instruction
and wait.

Evidence is written to the evidence directory as JSON, one file per scenario,
stamped with the build, the mod version, the session id and the timestamp.
`docs/PROGRESS.md` records which scenarios have been run and against which
build. **An unrun scenario is listed as unrun**, never as passing.

## Preparing a test world

Use a throwaway save. Sandbox settings that make these scenarios tractable:

- Zombie population: low (except S13, which needs one nearby on purpose)
- Start with a backpack, a tinned food item, a water bottle and a skill book
- Daylight, indoors, no fire

Run `pz-agent backup-save` first anyway. The whole point of the backup
subsystem is that you do not have to trust the agent to earn it.

## The scenarios

| ID | Scenario | Closes |
| --- | --- | --- |
| S01 | Heartbeat | Bridge is alive and reports the real build |
| S02 | Panic stop | Stop works and touches only mod-owned entries |
| S03 | Move three tiles | Movement verified by position, not by "we sent it" |
| S04 | Backpack → main inventory | Transfer verified in the destination container |
| S05 | Eat from backpack | Hunger actually dropped |
| S06 | Drink | Thirst or volume actually dropped |
| S07 | Read | Reading started and progressed |
| S08 | Cancel reading | Cancellation is clean and preserves progress |
| S09 | Manual takeover | The player always wins |
| S10 | Stale sidecar | Loss of the sidecar stops new work |
| S11 | Invalid item ref | A bad reference is refused, not acted on |
| S12 | Path blocked | Bounded replanning, honest failure |
| S13 | Zombie interruption | The reflex guard fires at the threshold |
| S14 | Backup / restore | Restore refuses while the game is open |
| S15 | Restart recovery | No command replay; re-arm required |

## Why these fifteen

They are not a feature tour. Each one is a place where a plausible
implementation passes its unit tests and still gets the real thing wrong:

- **S02 and S09** are the two ways the agent could take control away from you.
- **S05, S06, S07** are the three places "queued" could be mistaken for "done".
- **S10, S15** are the two ways a crash could leave the agent armed.
- **S11, S12** are the two ways a stale reference could act on the wrong object.
- **S13** is the difference between a guard that fires and one that fires late.
- **S14** is the only thing standing between a bug and your save.

## Endurance

Beyond the fifteen, `S99_endurance.yaml` runs at least 30 minutes of real time
in a safe world and asserts **absences**: no infinite loop, no command replay,
no unbounded log growth, no lost control, no false success, no save corruption.

Absences only show up over time. A ring buffer that grows by one entry per
rotation looks perfect for the first five minutes.
