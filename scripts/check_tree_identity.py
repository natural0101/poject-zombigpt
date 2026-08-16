#!/usr/bin/env python3
"""Name the tree ``scripts/check.sh`` just judged, so its verdict has a subject.

The gate's own header claimed "CI runs exactly these steps, in this order, so a
green run here means a green run there", and its last line was the unqualified
``All checks passed.`` Neither named a tree. A green run over a working tree
with uncommitted work is a true statement about a tree that will never exist as
a commit, and CI judges commits — so the two verdicts are about different things
and the first was being read as the second.

That is not hypothetical. On 2026-08-16 the gate printed ``All checks passed.``
here, the commit made from that tree was pushed, and CI went red on it: at
``c4b08ef`` ``docs/control/STATUS.json`` still described the previous commit,
which ``scripts/check_master_plan.py`` refuses. Running the gate *after* the
code commit is exactly what AGENTS.md requires and exactly what did not happen,
and the unqualified success line is what made skipping it feel safe.

So this prints the subject of the sentence. Three states, and only three:

* a clean tree — the verdict is about that commit, which is what CI will judge;
* a tree differing only inside ``docs/control/`` — the prescribed state between
  the code commit and the STATUS commit, where the pending commit's tree *is*
  this tree, so the verdict carries to it;
* anything else — the verdict is about no commit at all.

It never fails the gate. A checker that refuses a developer's ordinary
mid-change run gets argued with once and switched off, and there is nothing
wrong with running the gate over uncommitted work — the defect was calling that
run a verdict about a commit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: The one directory a pending commit may touch without making this run a
#: verdict about nothing. ``reconcile_status.py`` writes here between the code
#: commit and the STATUS commit, and ``check_master_plan.py`` grants the same
#: exemption for the same reason.
CONTROL_PLANE: Final = "docs/control/"


class Subject(Enum):
    """What the run's verdict is about."""

    COMMIT = "commit"
    PENDING_STATUS = "pending-status"
    NO_COMMIT = "no-commit"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Verdict:
    subject: Subject
    sentence: str


def judge(head: str, dirty: Sequence[str]) -> Verdict:
    """Classify a run from the HEAD sha and the paths that differ from it.

    Pure, so both branches can be exercised without building a repository for
    each; :func:`read_tree` is the half that talks to git and is covered by a
    run against a real one.
    """
    if not head:
        return Verdict(
            Subject.UNKNOWN,
            "no commit could be read, so this run is a verdict about an unnamed tree",
        )
    short = head[:8]
    outside = sorted(p for p in dirty if not p.startswith(CONTROL_PLANE))
    if not dirty:
        return Verdict(
            Subject.COMMIT,
            f"this run judged commit {short}, which is the tree CI will judge",
        )
    if not outside:
        return Verdict(
            Subject.PENDING_STATUS,
            f"this run judged a tree differing from {short} only inside {CONTROL_PLANE} — "
            "the STATUS commit still to be made, whose tree is this one, so the verdict "
            "carries to it",
        )
    return Verdict(
        Subject.NO_COMMIT,
        f"this run judged no commit: {len(outside)} path(s) outside {CONTROL_PLANE} differ "
        f"from {short}, first {outside[0]}. CI judges what you commit, not this — commit, "
        "then run the gate again",
    )


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    return result.stdout if result.returncode == 0 else ""


def _porcelain_paths(porcelain: str) -> list[str]:
    """Every path named by ``git status --porcelain``, renames included.

    A rename prints ``R  old -> new``; both halves are changes, and taking only
    the first would let a file moved out of ``docs/control/`` read as if it had
    stayed there.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        payload = line[3:]
        for part in payload.split(" -> "):
            cleaned = part.strip().strip('"')
            if cleaned:
                paths.append(cleaned)
    return paths


def read_tree() -> Verdict:
    head = _git("rev-parse", "HEAD").strip()
    # ``--untracked-files=all``, not the default: git collapses a wholly
    # untracked directory to the directory itself, so a new file under
    # ``docs/control/`` in a tree where nothing there is tracked yet prints as
    # ``?? docs/``. That is an *ancestor* of the exempt path, so the prefix
    # comparison reads it as outside and the prescribed in-between state gets
    # called a verdict about no commit. Measured against a real repository, not
    # reasoned about — the classification must never depend on how git chooses
    # to abbreviate its display.
    porcelain = _git("status", "--porcelain", "--untracked-files=all")
    return judge(head, _porcelain_paths(porcelain))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default="",
        help="text printed before the sentence, e.g. 'All checks passed'",
    )
    args = parser.parse_args()

    verdict = read_tree()
    if args.prefix:
        print(f"{args.prefix} — {verdict.sentence}.")
    else:
        print(f"{verdict.sentence}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
