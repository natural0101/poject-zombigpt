"""The block ``pz-agent start`` prints must be JSON on the machine that prints it.

It was assembled from f-strings, so the interpreter path went into a quoted
string with whatever separators it had. On Windows that produces

    "command": "C:\\Users\\Иван\\AppData\\Local\\Programs\\Python\\python.exe"

with single backslashes, which is not JSON — ``json.loads`` refuses it with
*Invalid \\escape*. The one configuration the product actually hands a user was
unparseable on the operating system the release is for, and it is the block a
user pastes into their client, so the failure lands on their first attempt to
use the MCP server at all.

The fix is not a smarter escape: it is building a dictionary and letting
:func:`json.dumps` encode it, which is correct for paths nobody thought of. That
is what these tests hold — every case below is a real Windows path shape, and
each is asserted to survive a round trip rather than to look right.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli import app
from pz_agent_cli.app import _mcp_snippet, mcp_client_entry
from pz_agent_cli.config import default_config
from pz_agent_cli.context import resolve_workspace
from pz_agent_cli.supervisor import SidecarSupervisor
from tests.fixtures.cli_worlds import make_world

#: Interpreter paths that break naive string assembly. Each is a real shape: a
#: default Windows install, one under Program Files, a Cyrillic account name,
#: and one with a space and a quote character in it.
INTERPRETERS: Final[tuple[str, ...]] = (
    r"C:\Users\Иван\AppData\Local\Programs\Python\Python312\python.exe",
    r"C:\Program Files\Python312\python.exe",
    r"C:\Users\John Smith\venv\Scripts\python.exe",
    r"D:\tools\py\"odd\python.exe",
    "/usr/bin/python3",
)


def _entry(tmp_path: Path, interpreter: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    world = make_world(tmp_path)
    monkeypatch.setattr("pz_agent_cli.app.sys.executable", interpreter)
    document = mcp_client_entry(resolve_workspace(world.ctx), redacted=False)
    served = document["pz-agent"]
    assert isinstance(served, dict)
    return served


@pytest.mark.parametrize("interpreter", INTERPRETERS, ids=lambda value: value[:28])
def test_the_printed_block_parses_as_json_for_any_interpreter_path(
    interpreter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered exactly as ``start`` prints it, then parsed back."""
    world = make_world(tmp_path)
    monkeypatch.setattr("pz_agent_cli.app.sys.executable", interpreter)

    block = "\n".join(_mcp_snippet(resolve_workspace(world.ctx), redacted=False))

    parsed = json.loads("{" + block + "}")
    assert parsed["pz-agent"]["command"] == interpreter, (
        "the path did not survive the round trip through the printed block"
    )


@pytest.mark.parametrize("interpreter", INTERPRETERS, ids=lambda value: value[:28])
def test_the_document_carries_the_path_unchanged(
    interpreter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dictionary, so there is nothing to escape by hand and nothing to get wrong."""
    entry = _entry(tmp_path, interpreter, monkeypatch)

    assert entry["command"] == interpreter


def test_the_server_is_told_which_workspace_to_attach_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client launches the server detached, with its own environment.

    Discovery cannot be relied on to reach the same workspace the sidecar is
    using, and the environment variable that used to be printed here was read by
    nothing. An argument is what carries it.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    monkeypatch.setattr("pz_agent_cli.app.sys.executable", INTERPRETERS[0])

    entry = mcp_client_entry(workspace, redacted=False)["pz-agent"]

    assert isinstance(entry, dict)
    args = entry["args"]
    assert isinstance(args, list)
    assert args[:2] == ["-m", "pz_agent_mcp"]
    assert "--state-dir" in args
    assert args[args.index("--state-dir") + 1] == str(workspace.state_dir)
    assert entry["env"] == {}


def test_the_redacted_form_is_still_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``--json`` form goes into bug reports, so it is parsed by a reader too."""
    world = make_world(tmp_path)
    monkeypatch.setattr("pz_agent_cli.app.sys.executable", INTERPRETERS[0])

    block = "\n".join(_mcp_snippet(resolve_workspace(world.ctx), redacted=True))

    parsed = json.loads("{" + block + "}")
    assert "Иван" not in json.dumps(parsed, ensure_ascii=False), "the account name survived"


def test_start_prints_a_block_a_client_can_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real command, because printing it is the point.

    The detached path is the one that prints the block — a foreground sidecar
    holds the terminal — so the spawner is faked rather than the printing, and
    what is parsed is the text a user would have copied off their screen.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")
    monkeypatch.setattr("pz_agent_cli.app.sys.executable", INTERPRETERS[0])
    monkeypatch.setattr(
        app,
        "build_supervisor",
        lambda ctx, ws: SidecarSupervisor(
            workspace.state_dir, clock=world.clock, spawn=lambda argv, cwd, log_path: 5150
        ),
    )

    world.reset_streams()
    assert world.run("start", "--json") == 0, world.stderr

    block = "\n".join(json.loads(world.stdout)["mcp"])
    entry = json.loads("{" + block + "}")["pz-agent"]
    assert entry["args"][:2] == ["-m", "pz_agent_mcp"]
    assert "--state-dir" in entry["args"]
