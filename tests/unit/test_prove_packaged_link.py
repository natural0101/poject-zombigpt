"""The packaged-link driver, exercised for real on the platform the tests run on.

``packaging/windows/prove_packaged_link.py`` exists to prove that the two
packaged executables complete an MCP initialize over the Core RPC link. CI runs
it against PyInstaller binaries; here the same driver runs against the same
product started as ``python -m pz_agent_cli`` and ``python -m pz_agent_mcp``,
which is the multi-word command shape its repeatable flags exist for. What is
asserted is the driver's whole contract, from outside: exit 0 with the success
line only when the initialize result was observed, and a nonzero exit with a
refusal naming the phase when the sidecar cannot start at all.

The driver is run as a subprocess rather than imported, because its exit code
and its two streams *are* the interface the CI step reads — an in-process call
could pass while the script, run as a program, printed to the wrong stream or
returned the wrong number. Every run is bounded by a subprocess timeout well
inside pytest's own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

import pz_agent_cli
import pz_agent_core
import pz_agent_mcp
import pz_agent_voice

#: Outer bound on one driver run. The driver's own internal deadlines sum to
#: well under this, so hitting it means the driver itself failed to stay
#: bounded — which is exactly a thing this test must catch.
GRACE: Final = 120.0

#: Hand-written rather than imported from the driver. This line is what the CI
#: step's reader greps for, so it is pinned here the way a wire string is: a
#: constant renamed in both halves at once is exactly the change an import
#: cannot see.
SUCCESS_PREFIX: Final = "OK: the packaged pair completed an MCP initialize over the RPC link"

DRIVER: Final = (
    Path(__file__).resolve().parents[2] / "packaging" / "windows" / "prove_packaged_link.py"
)


def _sdk_present() -> bool:
    try:
        import mcp  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


#: The positive proof needs the stdio server, which needs the optional extra.
#: The negative test does not: the sidecar refuses before MCP is ever launched.
_HAS_SDK = pytest.mark.skipif(
    not _sdk_present(),
    reason="the mcp extra is not installed; the stdio server cannot be exercised",
)


def _cli_command() -> list[str]:
    """How to run the real installed CLI, verified rather than assumed.

    ``python -m pz_agent_cli`` is preferred — it is what the product's own
    ``_sidecar_argv`` composes — and is only chosen after checking the package
    really has a ``__main__``. Where it does not, the console script installed
    next to this interpreter is the fallback, and its absence too is a plain
    failure rather than a guess.
    """
    package_dir = Path(pz_agent_cli.__file__ or "").resolve().parent
    if (package_dir / "__main__.py").is_file():
        return [sys.executable, "-m", "pz_agent_cli"]
    script = Path(sys.executable).with_name("pz-agent")
    assert script.is_file(), "neither pz_agent_cli.__main__ nor a pz-agent console script exists"
    return [str(script)]


def _driver_env() -> dict[str, str]:
    """The import path the driver's children need, derived from where we found the packages.

    pytest puts ``packages/*/src`` on this process's ``sys.path`` via the
    ``pythonpath`` setting, but that never reaches a grandchild process — the
    sidecar and the MCP server the driver spawns import from ``PYTHONPATH`` or
    not at all. Derived from the imported modules rather than hard-coded, so an
    installed distribution answers with its own locations.
    """
    roots = {
        str(Path(module.__file__ or "").resolve().parents[1])
        for module in (pz_agent_cli, pz_agent_core, pz_agent_mcp, pz_agent_voice)
    }
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*sorted(roots), *([existing] if existing else [])])
    return env


def _run_driver(
    scratch: Path, *, pz_agent: list[str], pz_agent_mcp: list[str]
) -> subprocess.CompletedProcess[str]:
    # The '=' spelling throughout: tokens like '-m' begin with a dash, and the
    # driver documents this as the way argparse accepts them.
    argv = [sys.executable, str(DRIVER), "--scratch", str(scratch)]
    argv += [f"--pz-agent={token}" for token in pz_agent]
    argv += [f"--pz-agent-mcp={token}" for token in pz_agent_mcp]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=GRACE,
        stdin=subprocess.DEVNULL,
        env=_driver_env(),
        check=False,
    )


@_HAS_SDK
def test_link_proof_end_to_end(tmp_path: Path) -> None:
    """The success path: a real sidecar, a real MCP child, an observed result.

    The scratch directory sits directly under ``tmp_path`` with a two-letter
    name because the state directory inside it becomes an ``AF_UNIX`` socket
    path on POSIX, and ``sun_path`` grants about a hundred bytes for the whole
    thing.
    """
    result = _run_driver(
        tmp_path / "lp",
        pz_agent=_cli_command(),
        pz_agent_mcp=[sys.executable, "-m", "pz_agent_mcp"],
    )

    assert result.returncode == 0, f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    assert SUCCESS_PREFIX in result.stdout
    assert "REFUSED" not in result.stdout + result.stderr


def test_a_refusing_sidecar_is_a_named_refusal(tmp_path: Path) -> None:
    """The honest failure: a sidecar that exits at once is named, fast, nonzero.

    The command is a real interpreter that exits 7 immediately, so the driver
    meets exactly what a broken packaged binary produces: a child that dies
    before any descriptor exists. The refusal must name the phase — that is
    what a CI reader greps for — and must arrive without waiting out the whole
    descriptor deadline, though only the outer bound enforces that here.
    """
    result = _run_driver(
        tmp_path / "neg",
        pz_agent=[sys.executable, "-c", "import sys; sys.exit(7)"],
        pz_agent_mcp=[sys.executable, "-m", "pz_agent_mcp"],
    )

    assert result.returncode != 0
    assert "REFUSED: sidecar-start" in result.stderr
    assert "exited with code 7" in result.stderr
    assert result.stdout == "", "a refusal must not also print a success stream"
