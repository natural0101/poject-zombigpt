#!/usr/bin/env python3
"""Count the progress of the 100-step plan and print the report block.

``overall_percent`` is the number of steps whose status is ``PASS`` — computed
here, written back into ``docs/control/STATUS.json`` by ``--write``, and checked
by :mod:`check_progress`. Three bands are counted separately because they are
separately blocked, and collapsing them is how "95% remote, 0% live" becomes
"nearly done".

Run it with no arguments to print. Run it with ``--write`` to recount and store.

**This counts ``docs/control/PLAN.md`` and nothing else.** The plan of record
moved to ``docs/control/MASTER_PLAN.yaml`` — 484 weighted tasks rather than 100
equal steps — and ``STATUS.json`` was regenerated in the new shape, which has no
``steps`` key at all. Every read below is a ``.get`` with a default, so against
the new file this printed a complete, confident, entirely false report: ``0%``
at a commit the file recorded as ``73.31%``, ``NOT_STARTED`` at step 1, ``RC
ARTIFACT: None`` beside a fully identified archive, ``LIVE SCENARIOS: 0/20``
against a catalogue of 22. ``--write`` was worse: it stored ``overall_percent:
0`` and six more zeroed keys into the file whose own ``$comment`` forbids a
hand-written value, next to the correct ``weighted_progress_percent``.

So the first thing this does is ask ``STATUS.json`` which plan it describes, and
refuse when the answer is not this one. A counter with nothing to count must say
so; falling back to zero is the defect, not the safe default.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
STATUS_PATH: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"

#: The plan this script counts, as ``STATUS.json`` spells it in ``plan_of_record``.
REPORTS_ON: Final = "docs/control/PLAN.md"

#: Which counter belongs to which plan, so a refusal sends the reader somewhere
#: rather than only saying no.
COUNTERS: Final = {
    "docs/control/MASTER_PLAN.yaml": "scripts/master_report.py",
}

REMOTE_BAND: Final = range(1, 96)
LIVE_BAND: Final = range(96, 99)
RELEASE_BAND: Final = range(99, 101)


def _require_the_plan_this_counts(document: dict[str, Any]) -> None:
    """Refuse a status file that is about a different plan.

    Checked before anything is counted and before ``--write`` can store
    anything, because both of those are how the mismatch became a number a
    person would read.
    """
    recorded = str(document.get("plan_of_record") or "")
    if recorded == REPORTS_ON:
        return
    successor = COUNTERS.get(recorded)
    where = f"Count that one with {successor}." if successor else "There is nothing here to count."
    raise SystemExit(
        f"{STATUS_PATH}: plan of record is "
        f"{recorded or 'unnamed'}, and this counts {REPORTS_ON}. {where}"
    )


def _load() -> dict[str, Any]:
    document: Any = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{STATUS_PATH}: must be a JSON object")
    _require_the_plan_this_counts(document)
    return document


def _counted(document: dict[str, Any]) -> dict[str, int]:
    passed = {
        int(step["id"])
        for step in document.get("steps", [])
        if isinstance(step, dict) and step.get("status") == "PASS"
    }
    return {
        "overall_percent": len(passed),
        "remote_percent": len(passed & set(REMOTE_BAND)),
        "live_game_percent": len(passed & set(LIVE_BAND)),
        "release_percent": len(passed & set(RELEASE_BAND)),
    }


def _next_step(document: dict[str, Any]) -> int:
    passed = {
        int(step["id"])
        for step in document.get("steps", [])
        if isinstance(step, dict) and step.get("status") == "PASS"
    }
    for number in range(1, 101):
        if number not in passed:
            return number
    return 100


def _render(document: dict[str, Any], counts: dict[str, int]) -> str:
    steps = {int(step["id"]): step for step in document.get("steps", [])}
    following = _next_step(document)
    current = steps.get(following, {})
    blockers = document.get("blockers") or []
    linux = document.get("linux_ci", {})
    windows = document.get("windows_ci", {})
    artifact = document.get("rc_artifact", {})
    live = document.get("live_scenarios", {})
    evidence: list[str] = []
    for step in document.get("steps", []):
        if isinstance(step, dict) and step.get("status") == "PASS":
            evidence.extend(str(item) for item in step.get("evidence") or [])
    return "\n".join(
        [
            f"PROGRESS: {counts['overall_percent']}%",
            f"STEP: {following}/100",
            f"STATUS: {current.get('status', 'NOT_STARTED')}",
            f"BRANCH: {document.get('branch')}",
            f"COMMIT: {document.get('head_commit')}",
            f"LINUX CI: {linux.get('status')} {linux.get('url') or ''}".rstrip(),
            f"WINDOWS CI: {windows.get('status')} {windows.get('url') or ''}".rstrip(),
            f"RC ARTIFACT: {artifact.get('status')} {artifact.get('url') or ''}".rstrip(),
            # Counted from the tally rather than the literal 20 that used to sit
            # here: the catalogue defines 22, so the literal was already wrong by
            # two and would have gone on reading as a full denominator.
            f"LIVE SCENARIOS: {live.get('passed', 0)}/"
            f"{sum(int(live.get(k, 0)) for k in ('passed', 'failed', 'not_run')) or '?'}",
            f"BLOCKERS: {', '.join(blockers) if blockers else 'none'}",
            f"EVIDENCE: {len(evidence)} path(s) recorded in docs/control/EVIDENCE_INDEX.md",
            f"NEXT STEP: {following} — {current.get('title', 'unknown')}",
            "",
            f"REMOTE IMPLEMENTATION: {counts['remote_percent']}/95",
            f"LIVE GAME VALIDATION: {counts['live_game_percent']}/3",
            f"FINAL RELEASE: {counts['release_percent']}/2",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="recount and store the percentages")
    parser.add_argument("--json", action="store_true", help="print the counted values as JSON")
    args = parser.parse_args()

    document = _load()
    counts = _counted(document)
    if args.write:
        document.update(counts)
        document["last_passed_step"] = max(
            (
                int(step["id"])
                for step in document.get("steps", [])
                if isinstance(step, dict) and step.get("status") == "PASS"
            ),
            default=0,
        )
        document["next_step"] = _next_step(document)
        document["current_step"] = document["next_step"]
        STATUS_PATH.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
        return 0
    print(_render(document, counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
