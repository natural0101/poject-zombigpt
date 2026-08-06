# evidence/

Where the twenty live scenarios leave what they observed. Every directory here
is written by `pz-agent live-test`; nothing in it is written by hand.

```
evidence/
  schema/
    result.schema.json      what a scenario result must look like
    manifest.schema.json    what the release manifest must look like
  S01_INSTALL/
    state.json              the attempt ledger — appended to, never rewritten
    result.json             the verdict-bearing result (a copy of one attempt)
    attempts/
      result.001.json       one file per attempt, kept forever
    logs/                   the logs this scenario declares
    journals/               command queue, acks, observation events
    snapshots/              session, capabilities, snapshot slots and pointer
    screenshots/            for the scenarios that require one
  ...
  S20_AUTONOMOUS_2_HOURS/
```

## The six commands

| Command | What it does |
|---|---|
| `pz-agent live-test prepare --save <mode>/<name>` | Builds the tree; refuses unless a *test* save exists and a backup of it verifies |
| `pz-agent live-test run --scenario S07_NESTED_INVENTORY --observations <file>` | Runs one scenario |
| `pz-agent live-test status` | Every scenario, its state, when it last ran |
| `pz-agent live-test resume` | Continues from the first scenario that is not PASS |
| `pz-agent live-test collect --scenario S07_NESTED_INVENTORY` | Copies logs, journals and snapshots into the scenario folder |
| `pz-agent live-test finalize` | Builds `release/evidence-manifest.json`, or names everything missing |

## How a scenario reaches PASS

Only one way: every postcondition it declares read a value that was present and
satisfied its check. The checks are a closed vocabulary — `observed`, `is_true`,
`is_false`, `equals`, `at_least`, `at_most`, `increased`, `decreased`,
`changed`, `unchanged` — and every one of them fails on a field that was not
observed. There is no check that passes on an absent value, and there is no
field anywhere in the input format that means "this succeeded".

`prepare` refuses to name a save whose name does not contain `test`, and refuses
a save whose newest backup does not verify. Neither refusal has an override
flag: the flag is what somebody passes at two in the morning to get past it.

## Observations

`live-test run` does not drive the game. The sidecar does, and this process does
not own that session — so a run with nothing to observe records a `BLOCKED`
attempt naming what it lacked, rather than a pass it cannot support.

You drive the scenario in-game with the sidecar attached, read the values back
(`pz-agent status --json`, the ack journal, the snapshot), and hand them over:

```json
{
  "scenario_id": "S07_NESTED_INVENTORY",
  "game_build": "42.20",
  "before":       { "inventory": { "main_count": 4 } },
  "after":        { "inventory": { "main_count": 5 } },
  "observations": {
    "transfer": { "item_in_main_after": true, "item_in_source_after": false },
    "action_result": { "reason_code": "POSTCONDITION_MET" }
  },
  "latencies_ms": [820, 910, 780],
  "logs": ["console.txt"],
  "screenshots": [],
  "failure_code": "",
  "detail": ""
}
```

`before` and `after` are read by the snapshot checks (`increased`, `decreased`,
`changed`, `unchanged`) using the same dotted path on both. `observations` is
read by all the others. A field the scenario names and the document does not
carry fails that postcondition — it does not default, and it does not skip.

There is no `status` field, no `passed` field and no `result` field, and adding
one would not help: the runner never reads them.

## What the hashes do, and what they do not

Every result is written through one canonical serialisation, so its SHA-256 is a
property of its content. That digest is recorded in the scenario's `state.json`
when the result is written, and re-checked on every read. Each ledger entry also
carries a hash chained over the entry before it, so editing an old attempt
invalidates every attempt after it.

**This is a tripwire, not a seal.** There is no key — this project ships no
secrets, anywhere — so somebody who edits `result.json` *and* recomputes the
whole ledger will not be caught by arithmetic. What it catches is the edit
people actually make: a result opened in a text editor to turn a `FAIL` into a
`PASS`. `finalize` reports that as tampering and refuses to build a manifest.

If you want a scenario to pass, run it until it does. A result that was edited
is evidence of nothing, and the next person builds on it.

## finalize

`finalize` writes `release/evidence-manifest.json` with a SHA-256 for every
artefact. It refuses, and writes nothing at all, when any of these hold:

- a scenario is not `PASS`;
- a required artefact is missing or empty — including a declared log that
  `collect` never found, and a screenshot for a scenario that requires one;
- a `result.json` no longer matches the digest recorded for it.

It names every problem in one pass. A partial manifest is not written, because a
partial manifest is exactly the artefact a release gate would accept.
