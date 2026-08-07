#!/usr/bin/env python3
"""Count the progress and print the report block. The count is never written by hand.

``overall_percent`` is the number of steps whose status is ``PASS`` — computed
here, written back into ``docs/control/STATUS.json`` by ``--write``, and checked
by :mod:`check_progress`. Three bands are counted separately because they are
separately blocked, and collapsing them is how "95% remote, 0% live" becomes
"nearly done".

Run it with no arguments to print. Run it with ``--write`` to recount and store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
STATUS_PATH: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"

REMOTE_BAND: Final = range(1, 96)
LIVE_BAND: Final = range(96, 99)
RELEASE_BAND: Final = range(99, 101)


def _load() -> dict[str, Any]:
    document: Any = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{STATUS_PATH}: must be a JSON object")
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
            f"LIVE SCENARIOS: {live.get('passed', 0)}/20",
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
