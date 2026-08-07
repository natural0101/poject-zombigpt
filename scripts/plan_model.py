#!/usr/bin/env python3
"""The shape of ``docs/control/MASTER_PLAN.yaml``, and the rules it must obey.

Five levels, because four of them collapse into a number and the fifth is the
only one that is evidence:

``EPIC`` → ``MILESTONE`` → ``TASK`` → ``CHECK`` → ``EVIDENCE``

A ``TASK`` is one action. A ``CHECK`` is a statement about a milestone that no
single task establishes — usually an integration claim, the kind that is true of
the whole and of none of the parts. ``EVIDENCE`` is a path that exists on disk;
everything above it is a claim.

**Weight is not task count.** Writing a paragraph of documentation and getting a
live Project Zomboid scenario to pass are both "one task", and treating them as
equal is how a plan reports readiness it does not have. Weight is the whole
point of this model, so it is validated: a documentation task may not carry the
weight of a transport task, and neither may carry the weight of a live one.

**An epic does not close because its tasks did.** Five closing conditions, all
required, in :func:`epic_closed`. The one that matters most is the last: an
integration scenario that exercises the epic end to end. Every defect this
project has found was a subsystem that was complete, tested, green, and
connected to nothing — and a count of passing tasks is exactly the metric that
cannot see that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "BANDS",
    "METRICS",
    "STATUSES",
    "Check",
    "Epic",
    "Milestone",
    "Task",
    "epic_closed",
    "weight_band",
]

STATUSES: Final = ("NOT_STARTED", "IN_PROGRESS", "BLOCKED", "FAIL", "PASS")

#: What a weight means, so a number is a judgement rather than a habit. The
#: bands are disjoint on purpose: a task's kind decides its band, and a task
#: that could sit in two bands is a task that has not been split finely enough.
BANDS: Final = {
    "doc": (1, 2),
    "audit": (1, 3),
    "portability": (3, 5),
    "packaging": (3, 5),
    "evidence": (4, 6),
    "transport": (5, 6),
    "integration": (7, 8),
    "security": (7, 9),
    "live": (9, 10),
}

#: The seven readiness figures, each over its own epics. Reported separately
#: because a single number hides a subsystem at zero, and every one of these can
#: be at zero while the others are high.
METRICS: Final = {
    "CODE_IMPLEMENTATION": ("E03", "E05", "E06", "E08", "E09", "E10"),
    "WINDOWS_COMPATIBILITY": ("E02", "E04"),
    "MCP_OPERABILITY": ("E07",),
    "VOICE_OPERABILITY": ("E09", "E10"),
    "RC_PACKAGING": ("E11",),
    "LIVE_GAME_VALIDATION": ("E14",),
    "FINAL_RELEASE": ("E15",),
}


@dataclass(frozen=True, slots=True)
class Task:
    """One action, with everything needed to tell whether it happened."""

    id: str
    subsystem: str
    action: str
    weight: int
    band: str
    owner: str  # "remote" | "local"
    pass_criterion: str
    verify_command: str
    regression_test: str
    evidence: str
    depends_on: tuple[str, ...] = ()
    status: str = "NOT_STARTED"
    commit: str | None = None
    ci_url: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subsystem": self.subsystem,
            "action": self.action,
            "weight": self.weight,
            "band": self.band,
            "owner": self.owner,
            "depends_on": list(self.depends_on),
            "pass_criterion": self.pass_criterion,
            "verify_command": self.verify_command,
            "regression_test": self.regression_test,
            "evidence": self.evidence,
            "status": self.status,
            "commit": self.commit,
            "ci_url": self.ci_url,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Check:
    """A statement about a milestone that no single task establishes.

    The integration claim. A milestone whose every task passed and whose check
    did not is the exact shape of this project's recurring defect: the parts
    work and nothing joins them.
    """

    id: str
    statement: str
    command: str
    status: str = "NOT_STARTED"
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "command": self.command,
            "status": self.status,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    title: str
    tasks: tuple[Task, ...]
    checks: tuple[Check, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "tasks": [task.to_dict() for task in self.tasks],
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class Epic:
    id: str
    title: str
    subsystem: str
    #: The scenario that has to run end to end before this epic may close. Not a
    #: test name — a description of the thing being exercised, so that swapping
    #: in a unit test that happens to be green is visibly not it.
    integration_scenario: str
    #: Which CI workflow has to be green. None where no workflow can speak to it
    #: — the live epics, where CI is not the authority and cannot be.
    required_ci: str | None
    milestones: tuple[Milestone, ...] = ()

    @property
    def tasks(self) -> tuple[Task, ...]:
        return tuple(task for milestone in self.milestones for task in milestone.tasks)

    @property
    def checks(self) -> tuple[Check, ...]:
        return tuple(check for milestone in self.milestones for check in milestone.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "subsystem": self.subsystem,
            "integration_scenario": self.integration_scenario,
            "required_ci": self.required_ci,
            "milestones": [milestone.to_dict() for milestone in self.milestones],
        }


def weight_band(weight: int, band: str) -> bool:
    """Whether *weight* is legal for *band*."""
    low, high = BANDS[band]
    return low <= weight <= high


def epic_closed(epic: Epic, *, ci_green: bool) -> tuple[bool, str]:
    """Whether *epic* may be called closed, and why not if it may not.

    Five conditions, every one required. Deliberately not "enough tasks passed":
    a proportion is what lets an epic close over the top of the one task that
    was actually load bearing.
    """
    tasks = epic.tasks
    if not tasks:
        return False, "the epic has no tasks"
    unfinished = [task.id for task in tasks if task.status != "PASS"]
    if unfinished:
        return False, f"{len(unfinished)} task(s) are not PASS, first {unfinished[0]}"
    open_checks = [check.id for check in epic.checks if check.status != "PASS"]
    if open_checks:
        return False, f"{len(open_checks)} check(s) are not PASS, first {open_checks[0]}"
    without_evidence = [task.id for task in tasks if not task.evidence]
    if without_evidence:
        return False, f"{len(without_evidence)} task(s) carry no evidence path"
    if epic.required_ci is not None and not ci_green:
        return False, f"{epic.required_ci} is not green"
    if not epic.integration_scenario:
        return False, "the epic declares no integration scenario"
    return True, ""
