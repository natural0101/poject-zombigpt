#!/usr/bin/env python3
"""Regenerate the per-scenario half of ``docs/LIVE_TEST_PLAYBOOK.md``.

The playbook's own header says "this file is generated ... regenerate it rather
than editing it", and until now there was nothing to regenerate it *with*. A
document that claims to be generated, has no generator and has no check is a
document that drifts silently — which is the same defect as a subsystem nobody
connected, wearing different clothes.

The prose above the first scenario and below the last is hand-written and is
preserved byte for byte. Everything between is rendered from
:data:`pz_agent_cli.livetest.scenarios.SCENARIOS`, which is what the runner
executes.

    scripts/generate_playbook.py            rewrite the file
    scripts/generate_playbook.py --check    exit 1 if it would change

``--check`` is what ``scripts/check.sh`` runs, so a scenario edited without a
regenerate fails the gate rather than shipping a stale playbook.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
PLAYBOOK: Final = REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md"

for package in ("pz_agent_cli", "pz_agent_core", "pz_agent_mcp", "pz_agent_voice"):
    source = REPO_ROOT / "packages" / package / "src"
    if source.is_dir():
        sys.path.insert(0, str(source))

from pz_agent_cli.livetest.scenarios import SCENARIOS, LiveScenario  # noqa: E402

#: The line that opens the generated region, and the one that closes it. Found
#: rather than assumed: a hand-written preamble that grew a heading would
#: otherwise be silently swallowed.
FIRST_MARKER: Final = f"## {SCENARIOS[0].id}\n"
LAST_MARKER: Final = "---\n"


def _nest(paths: list[str]) -> dict[str, object]:
    """Turn dotted paths into the nested shape the runner reads them from."""
    document: dict[str, object] = {}
    for path in sorted(paths):
        cursor = document
        parts = path.split(".")
        for part in parts[:-1]:
            nested = cursor.setdefault(part, {})
            if not isinstance(nested, dict):  # pragma: no cover - a path prefixing another
                break
            cursor = nested
        else:
            cursor.setdefault(parts[-1], None)
    return document


def _observations_skeleton(scenario: LiveScenario) -> list[str]:
    """The exact document ``--observations`` expects, with the values left blank.

    The playbook told an operator to pass ``--observations <file>`` for every
    scenario and never said what the file had to contain. The runner reads each
    postcondition by an exact dotted path — sixty-six across the catalogue — and
    those paths existed only in ``scenarios.py``. An operator working from this
    document could not write a file the runner would read, and a wrong guess
    fails the postcondition, correctly and unhelpfully.

    ``null`` is deliberate: every check refuses an unread value, so a skeleton
    handed back unfilled produces an honest failure rather than a pass. It is a
    form to complete, not a default to accept.

    ``game_build`` is named separately in the prose because it is the one
    required field that is not a ``null`` and not a postcondition: nothing in
    the catalogue reads the top-level value, so "fill every ``null``" excluded
    exactly the field ``check_release.py`` refuses an archive over. The runner
    now refuses a PASS without it, at the scenario rather than at the release.
    """
    import json  # noqa: PLC0415

    snapshot = [c.field for c in scenario.postconditions if c.check.reads_snapshots]
    observed = [c.field for c in scenario.postconditions if not c.check.reads_snapshots]
    document: dict[str, object] = {"scenario_id": scenario.id, "game_build": ""}
    if observed:
        document["observations"] = _nest(observed)
    if snapshot:
        document["before"] = _nest(snapshot)
        document["after"] = _nest(snapshot)
    rendered = json.dumps(document, indent=2, ensure_ascii=False)
    return [
        "### The observations file\n",
        "\n",
        "Fill every `null` with what you read back, then pass it to `--observations`.\n",
        "A value left unread fails its postcondition — that is the point.\n",
        "\n",
        "`game_build` is required too, and it is not a `null`: write the build string\n",
        "the running game reports. No postcondition reads it, so a scenario would otherwise\n",
        "pass without it and the release gate would refuse the whole archive after all "
        f"{len(SCENARIOS)}\n",
        "sessions were spent. The runner refuses that PASS here instead.\n",
        "\n",
        "```json\n",
        *[f"{line}\n" for line in rendered.splitlines()],
        "```\n",
        "\n",
    ]


def _render(scenario: LiveScenario) -> str:
    out: list[str] = [
        f"## {scenario.id}\n",
        "\n",
        f"**{scenario.title}**\n",
        "\n",
        f"{scenario.goal}\n",
        "\n",
        "### Prepare the world\n",
        "\n",
    ]
    out += [f"- {line}\n" for line in scenario.world_preparation]
    out += ["\n", "### Required starting state\n", "\n"]
    out += [f"- {line}\n" for line in scenario.required_state]
    out += ["\n", "### Run\n", "\n", "```\n", f"{scenario.command}\n", "```\n", "\n"]
    out += ["### What you do in the game\n", "\n"]
    out += [f"{index}. {step}\n" for index, step in enumerate(scenario.operator_steps, start=1)]
    out += ["\n", "### Expected\n", "\n", f"{scenario.expected_result}\n", "\n"]
    out += [
        "### Required postconditions\n",
        "\n",
        "Every one of these must be observed. A missing one is not a pass.\n",
        "\n",
    ]
    for condition in scenario.postconditions:
        # The field path and the check, beside the prose. Without them the
        # playbook told an operator to pass ``--observations <file>`` and never
        # said what had to be in it: the runner reads each postcondition by an
        # exact dotted path, and those paths appeared nowhere outside
        # ``scenarios.py``. Sixty-six of them across the catalogue, every one to
        # be transcribed by hand on the machine with the game.
        where = "before/after" if condition.check.reads_snapshots else "observations"
        pin = f"`{where}.{condition.field}` · {condition.check.value}"
        if condition.expected is not None:
            pin += f" `{condition.expected!r}`"
        if condition.why:
            # Two trailing spaces: a markdown hard break, so the reason sits
            # under its postcondition rather than joining the next bullet.
            out.append(f"- **`{condition.key}`** — {condition.statement}  \n")
            out.append(f"  {pin}  \n")
            out.append(f"  _{condition.why}_\n")
        else:
            out.append(f"- **`{condition.key}`** — {condition.statement}  \n")
            out.append(f"  {pin}\n")
    out.append("\n")
    out += _observations_skeleton(scenario)

    budget = f"**Time budget:** {scenario.time_budget_s} s"
    if scenario.measures_latency:
        budget += "  ·  **latency measured** (p50/p95 recorded in `result.json`)"
    out += [f"{budget}\n", "\n"]

    evidence = f"**Evidence:** `evidence/{scenario.id}/`"
    if scenario.screenshots_required:
        evidence += "  — screenshots required"
    out += [f"{evidence}\n", "\n"]

    out += [
        "### When it fails\n",
        "\n",
        f"Look first at **{scenario.suspect_module}**.\n",
        "\n",
        "Logs to collect:\n",
        "\n",
    ]
    out += [f"- `{name}`\n" for name in scenario.logs]
    out.append("\n")
    return "".join(out)


def render_all() -> str:
    return "".join(_render(scenario) for scenario in SCENARIOS)


def rebuild(current: str) -> str:
    """*current* with its generated region replaced."""
    start = current.index(FIRST_MARKER)
    # The closing rule is the last one in the file; searching from the end keeps
    # a rule inside a scenario body from truncating the region.
    end = current.rindex(LAST_MARKER)
    return current[:start] + render_all() + current[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 when the file is stale",
    )
    args = parser.parse_args()

    current = PLAYBOOK.read_text(encoding="utf-8")
    rebuilt = rebuild(current)

    if current == rebuilt:
        print(f"{PLAYBOOK.relative_to(REPO_ROOT)} is up to date ({len(SCENARIOS)} scenarios).")
        return 0

    if args.check:
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rebuilt.splitlines(keepends=True),
            fromfile="docs/LIVE_TEST_PLAYBOOK.md",
            tofile="regenerated",
        )
        sys.stdout.writelines(diff)
        print("\nthe playbook does not match the scenarios; run scripts/generate_playbook.py")
        return 1

    PLAYBOOK.write_text(rebuilt, encoding="utf-8")
    print(f"rewrote {PLAYBOOK.relative_to(REPO_ROOT)} from {len(SCENARIOS)} scenarios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
