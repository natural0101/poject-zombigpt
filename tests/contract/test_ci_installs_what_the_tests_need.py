"""The two workflows must install the same project, or one of them tests less.

This file exists because of a specific failure, and the failure is worth writing
down because it did not look like one.

``tests/unit/test_mcp_entry.py`` and ``tests/contract/test_mcp_subprocess_e2e.py``
run the real ``pz-agent-mcp`` entry point and assert the exit code it gives for a
real refusal: ``EXIT_NOT_WIRED`` for a missing descriptor, ``EXIT_STALE_DESCRIPTOR``
for one whose process is gone, and so on. All of that is downstream of one check
the entry point makes first — is the MCP SDK importable? If it is not, the entry
point answers ``EXIT_NO_SDK`` and returns, and every assertion below compares that
one code against the one it meant to check.

``windows.yml`` installed ``.[dev,mcp]``; ``ci.yml`` installed ``.[dev]``. So the
same commit was green on Windows and red on Linux with thirty-four failures, none
of which were about the code under test. A local ``scripts/check.sh`` was green
too, because a developer venv had the SDK in it. Three environments, three answers,
one of them wrong for a reason none of the three reported.

The rule this file enforces is therefore not "install mcp". It is that the two
workflows install the **same** extras, checked by reading them out of the YAML —
because the divergence, not the missing package, is what made the failure
invisible. A future extra added to one workflow and not the other fails here
rather than three commits later on the platform nobody was watching.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

import pz_agent_core
import pz_agent_mcp
from pz_agent_mcp import __main__ as entry

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
CI: Final = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WINDOWS: Final = REPO_ROOT / ".github" / "workflows" / "windows.yml"

#: An editable install of this project with its extras, in either workflow's
#: spelling: ``uv pip install --python .venv/bin/python -e ".[dev,mcp]"`` and
#: ``python -m pip install -e ".[dev,mcp]" "pyinstaller>=6,<7"`` both match.
_EDITABLE_INSTALL: Final = re.compile(r"""-e\s+["']\.\[([^\]]*)\]["']""")

#: The entry point returns this before it looks at anything else. Read from the
#: module rather than written here, because a test that pins a number to itself
#: proves the number, not the behaviour.
_NO_SDK_MESSAGE: Final = "the MCP SDK is not installed"


def _extras(workflow: Path) -> frozenset[str]:
    """Every extra this workflow installs the project with.

    A workflow may install more than once (a matrix leg, a second job); the union
    is what the runner ends up with, so the union is what is compared.
    """
    text = workflow.read_text(encoding="utf-8")
    found = _EDITABLE_INSTALL.findall(text)
    assert found, (
        f"{workflow.name}: no editable install of this project was found. Either the "
        f"workflow stopped installing the project — in which case its tests are running "
        f"against something else — or the pattern in {Path(__file__).name} no longer "
        f"matches how it is spelled, which would make every assertion below vacuous."
    )
    return frozenset(
        extra.strip() for group in found for extra in group.split(",") if extra.strip()
    )


@pytest.mark.parametrize("workflow", [CI, WINDOWS], ids=lambda path: path.name)
def test_the_workflow_exists_and_installs_this_project(workflow: Path) -> None:
    """Guard the guard: the files this module reads have to be there."""
    assert workflow.is_file(), f"{workflow} is missing"
    assert _extras(workflow), f"{workflow.name}: installs the project with no extras at all"


def test_both_workflows_install_the_same_extras() -> None:
    """The divergence itself is the defect, whichever direction it runs in."""
    linux = _extras(CI)
    windows = _extras(WINDOWS)
    assert linux == windows, (
        "the two workflows install different extras, so one of them is testing a "
        "smaller surface than the other and the difference will show up as a "
        "platform-specific failure that is not about the platform.\n"
        f"  only in {CI.name}:      {sorted(linux - windows)}\n"
        f"  only in {WINDOWS.name}: {sorted(windows - linux)}"
    )


def test_both_workflows_install_the_mcp_extra() -> None:
    """Without it the entry-point suites assert against a refusal they never meant."""
    for workflow in (CI, WINDOWS):
        assert "mcp" in _extras(workflow), (
            f"{workflow.name} does not install the mcp extra. Every test that runs "
            f"pz-agent-mcp and asserts an exit code will get EXIT_NO_SDK instead, and "
            f"the run will be red for a reason that has nothing to do with the code."
        )


def test_the_entry_point_really_does_refuse_without_the_sdk(tmp_path: Path) -> None:
    """The premise of this whole file, observed rather than assumed.

    Run the real entry point with the SDK made unimportable and watch it answer
    ``EXIT_NO_SDK``. Grepping the source for the message would have proved that a
    string exists; this proves that the short circuit happens, which is the thing
    that made thirty-four unrelated assertions compare against the wrong number.

    The SDK is hidden by shadowing it with a package that raises ``ImportError``
    on import, which is exactly what :func:`~pz_agent_mcp.server.require_sdk`
    catches. Nothing is uninstalled, so the run is repeatable and leaves no trace.
    """
    shadow = tmp_path / "shadow"
    (shadow / "mcp").mkdir(parents=True)
    (shadow / "mcp" / "__init__.py").write_text(
        "raise ImportError('hidden by tests/contract/test_ci_installs_what_the_tests_need.py')\n",
        encoding="utf-8",
    )

    # The shadow first, then wherever the packages were actually imported from —
    # derived, not spelled as packages/*/src, so this keeps working against an
    # installed distribution.
    roots = [
        str(shadow),
        *sorted(
            {
                str(Path(module.__file__ or "").resolve().parents[1])
                for module in (pz_agent_core, pz_agent_mcp)
            }
        ),
    ]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join([*roots, *([existing] if existing else [])])
    completed = subprocess.run(
        [sys.executable, "-m", "pz_agent_mcp", "--state-dir", str(tmp_path / "absent")],
        check=False,  # a non-zero exit is the observation, not an error
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        cwd=REPO_ROOT,
    )

    assert completed.returncode == entry.EXIT_NO_SDK, (
        "the entry point no longer short-circuits on a missing SDK. If that check is "
        "gone, re-derive whether the workflows still need the extra before deleting "
        f"this file.\n  exit: {completed.returncode}\n  stderr: {completed.stderr!r}"
    )
    assert _NO_SDK_MESSAGE in completed.stderr, (
        f"the refusal no longer names the missing install step: {completed.stderr!r}"
    )
    assert completed.stdout == "", "a refusal must leave stdout clean for the protocol"


def test_the_suites_that_depend_on_the_extra_are_actually_present() -> None:
    """Name the callers, so deleting them makes this file's rule visible as dead."""
    dependents = (
        REPO_ROOT / "tests" / "unit" / "test_mcp_entry.py",
        REPO_ROOT / "tests" / "contract" / "test_mcp_subprocess_e2e.py",
    )
    missing = [path.name for path in dependents if not path.is_file()]
    assert not missing, (
        f"the suites this rule exists for are gone: {missing}. The extras still have to "
        f"match, but the reason recorded in this module's docstring is now stale."
    )
