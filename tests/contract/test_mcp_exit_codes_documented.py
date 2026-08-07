"""The exit code a client author is told to expect must be the one they get.

``configs/mcp/README.md`` is a first-contact document: somebody wiring a client
reads it before anything else, and the first thing that happens to them is a
refusal. Which refusal decides what they do next — install an extra, or accept
that this build has no channel handing the sidecar's ports to a second process.

It described only the second. It said ``pz-agent-mcp`` "exits with status 1",
and on a plain install you get **3**, because the SDK gate fires first and the
message is about a missing package rather than a missing sidecar. Someone
following that paragraph would have gone looking for the wrong thing.

So both codes are pinned here against the constants, and the constants against
the behaviour. ``--describe`` is exercised for real, because it is the one
invocation the same document promises works with no game, no sidecar and no
SDK — and that promise is the reason the file is worth reading at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pz_agent_core
import pz_agent_mcp
from pz_agent_mcp.__main__ import EXIT_NO_SDK, EXIT_NOT_WIRED, EXIT_OK, EXIT_USAGE

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
CONFIG_README: Final = REPO_ROOT / "configs" / "mcp" / "README.md"

#: Every package the child needs on its import path, as objects rather than as
#: paths, so the location is read from the import system rather than guessed.
_PACKAGES: Final = (pz_agent_mcp, pz_agent_core)

#: ``**Exit 3 — the MCP SDK is not installed.**`` and its sibling.
_STATED: Final = re.compile(r"\*\*Exit (\d+) —")


def _documented_codes() -> set[int]:
    return {int(code) for code in _STATED.findall(CONFIG_README.read_text(encoding="utf-8"))}


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    """The server as a client launches it: a subprocess, stdin closed.

    The child gets its import path from wherever this process found the
    package, so the test holds whether the suite runs against an installed
    distribution or against the source tree with conftest injecting paths. A
    hard-coded ``packages/*/src`` would have passed here and told an installed
    environment nothing.
    """
    roots: set[str] = set()
    for module in _PACKAGES:
        located = module.__file__
        # A namespace package has no __file__. None of these is one, and saying
        # so here means a future one fails loudly rather than silently dropping
        # a path the child needs.
        assert located is not None, f"{module.__name__} has no file to locate it by"
        roots.add(str(Path(located).resolve().parents[1]))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([*sorted(roots), existing]) if existing else os.pathsep.join(sorted(roots))
    )
    return subprocess.run(
        [sys.executable, "-m", "pz_agent_mcp", *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env=env,
        check=False,
    )


def test_the_readme_names_both_refusals_not_only_one() -> None:
    """One gate documented out of two sends a reader after the wrong cause."""
    documented = _documented_codes()

    assert EXIT_NO_SDK in documented, "the SDK gate fires first and is undocumented"
    assert EXIT_NOT_WIRED in documented, "the no-services refusal is undocumented"


def test_every_code_the_readme_states_is_one_the_program_defines() -> None:
    """A stated code the program cannot produce is a remedy for nothing."""
    defined = {EXIT_OK, EXIT_NOT_WIRED, EXIT_USAGE, EXIT_NO_SDK}

    invented = sorted(_documented_codes() - defined)

    assert invented == [], "configs/mcp/README.md states exit codes the server never returns"


def test_a_bare_launch_refuses_with_a_documented_code_and_says_why() -> None:
    """Run, not asserted about. Whichever gate fires, both are documented now."""
    result = _run()

    assert result.returncode in _documented_codes(), (
        f"a bare launch exits {result.returncode}, which configs/mcp/README.md does not mention"
    )
    assert result.stderr.strip(), "a refusal with no explanation is not one"
    assert result.stdout == "", "nothing may reach stdout: a client is parsing it as protocol"


def test_describe_answers_with_no_game_no_sidecar_and_no_sdk() -> None:
    """The one promise the document makes about what works today.

    Checked by running it rather than by reading the catalogue, because the
    claim is about the *executable* answering, and an import that failed would
    satisfy any test that only inspected ``pz_agent_mcp.catalog``.
    """
    result = _run("--describe")

    assert result.returncode == EXIT_OK, result.stderr
    document = json.loads(result.stdout)
    assert document["tools"], "the described surface is empty"
    assert document["resources"], "the described surface names no resources"
    assert document["protocol_version"], "a client needs the protocol version to key on"
