"""The shipped MCP client configurations, checked against the server they launch.

A configuration file is a claim about a program: that this command exists, that
it accepts these arguments, that these tools will be there. None of those claims
is checked by the client — it just runs the command and reports whatever
happens — so they are checked here.

The strongest one is the last: every tool named in a configuration or in the
documentation is resolved against the catalogue *and* against a live
:class:`ToolRouter`, whose constructor refuses to exist if any published tool
has no handler. A tool named in prose and absent from the router is a promise
nobody kept, and it costs a client author an afternoon to discover.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.app import _mcp_snippet
from pz_agent_cli.context import resolve_workspace
from pz_agent_mcp.__main__ import build_parser
from pz_agent_mcp.catalog import RESOURCES_BY_URI, TOOLS_BY_NAME
from pz_agent_mcp.router import ToolRouter
from pz_agent_mcp.server import SERVER_NAME
from tests.fixtures.cli_worlds import make_world
from tests.fixtures.mcp_doubles import Doubles

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

CONFIG_DIR: Final = REPO_ROOT / "configs" / "mcp"

CONFIG_NAMES: Final[tuple[str, ...]] = (
    "claude-desktop.json",
    "claude-code.json",
    "generic-stdio.json",
)

#: Interpreters a configuration may name instead of the console script. Anything
#: else has to be a script this project declares.
_INTERPRETERS: Final[frozenset[str]] = frozenset({"python", "python3", "py"})

#: ``pz_agent_...`` is a package name; the tools are the rest.
_TOOL_TOKEN: Final = re.compile(r"\bpz_(?!agent)[a-z0-9_]+\b")

#: Anything shaped like a credential. None of these files may carry one.
_SECRET_SHAPES: Final[tuple[str, ...]] = ("sk-", "ghp_", "api_key", "token", "password", "secret")


def _load(name: str) -> dict[str, Any]:
    document: Any = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _entry(name: str) -> dict[str, Any]:
    servers = _load(name)["mcpServers"]
    assert list(servers) == [SERVER_NAME], f"{name} should configure exactly {SERVER_NAME}"
    entry: Any = servers[SERVER_NAME]
    assert isinstance(entry, dict)
    return entry


def _console_scripts() -> dict[str, str]:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts: dict[str, str] = document["project"]["scripts"]
    return scripts


def _module_dirs() -> set[str]:
    return {
        path.name
        for path in (REPO_ROOT / "packages").glob("*/src/*")
        if (path / "__init__.py").is_file()
    }


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_configuration_names_the_server_this_build_registers(name: str) -> None:
    entry = _entry(name)
    assert entry["command"]
    assert isinstance(entry["args"], list)


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_command_is_something_this_repository_provides(name: str) -> None:
    """Either the declared console script, or an interpreter running a real module."""
    entry = _entry(name)
    command = str(entry["command"])
    args = [str(item) for item in entry["args"]]
    if command in _INTERPRETERS:
        assert args[:1] == ["-m"], f"{name}: an interpreter needs -m <module>"
        assert args[1] in _module_dirs(), f"{name}: no such package in this tree"
        assert args[1] == "pz_agent_mcp"
        return
    scripts = _console_scripts()
    assert command in scripts, f"{name}: {command!r} is not a console script of this project"
    assert scripts[command].startswith("pz_agent_mcp"), f"{name}: {command!r} is not the server"


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_server_arguments_are_ones_the_server_accepts(name: str) -> None:
    """Whatever is left after the launch form must parse as server arguments."""
    entry = _entry(name)
    args = [str(item) for item in entry["args"]]
    if args[:1] == ["-m"]:
        args = args[2:]
    # parse_args exits on an unrecognised option, which is the failure worth
    # catching: an argument that looks plausible and is not accepted leaves the
    # client with a server that dies at launch.
    build_parser().parse_args(args)


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_configuration_sets_no_environment_variable(name: str) -> None:
    """The server reads none of its own, so naming one would be decoration.

    Discovery uses the standard Windows variables the client already passes
    down. A key invented here would look like configuration and do nothing,
    which is worse than an empty object that says exactly what is needed.
    """
    assert _entry(name).get("env") == {}


def test_the_configuration_the_cli_prints_sets_no_variable_either(tmp_path: Path) -> None:
    """The one a user actually pastes, which the pin above never saw.

    ``pz-agent start`` prints this block with real interpreter and state paths
    so it can be copied into a client, and it set ``PZ_AGENT_STATE_DIR`` — a
    name that occurred exactly once in the repository, in the literal that
    printed it. Nothing read it, and the three files checked above, their
    README's "Why ``env`` is empty" section and the test above all said the
    server reads none. The guard covered the checked-in files and stopped
    exactly where the product started handing one out.

    Parsed as JSON rather than matched as text: the block is what a client will
    parse, so a change that keeps the words and breaks the shape is a change
    this has to catch.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    block = "\n".join(_mcp_snippet(workspace, redacted=False))

    entry = json.loads("{" + block + "}")["pz-agent"]
    assert entry["env"] == {}, "the printed configuration names a variable nothing reads"
    assert entry["args"] == ["-m", "pz_agent_mcp"]
    assert entry["command"], "the block names no interpreter to launch"


def test_the_printed_configuration_agrees_with_the_shipped_ones(tmp_path: Path) -> None:
    """Two sources for one answer is how they drift; this is the comparison.

    The *launch form* is allowed to differ and does: two shipped files use the
    ``pz-agent-mcp`` console script, the third and the printed block use
    ``<interpreter> -m pz_agent_mcp``. Both reach the same module, which is what
    the checks above establish per file. What may not differ is ``env``, because
    that is a claim about what the server reads, and there is one answer to that.
    """
    world = make_world(tmp_path)
    printed = json.loads(
        "{" + "\n".join(_mcp_snippet(resolve_workspace(world.ctx), redacted=False)) + "}"
    )["pz-agent"]

    assert printed["args"][:2] == ["-m", "pz_agent_mcp"], (
        "the printed block launches something else"
    )
    for name in CONFIG_NAMES:
        assert _entry(name).get("env") == printed["env"], (
            f"{name} and the block pz-agent prints disagree about what the server reads"
        )


@pytest.mark.parametrize("name", (*CONFIG_NAMES, "README.md"))
def test_shipped_configuration_carries_no_credential(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8").lower()
    for shape in _SECRET_SHAPES:
        if shape in ("api_key", "token", "password", "secret") and name == "README.md":
            # The README is allowed to say the word while explaining that none
            # of them belongs in these files.
            continue
        assert shape not in text, f"{name} mentions {shape!r}"


# ---------------------------------------------------------------------------
# what the documentation promises
# ---------------------------------------------------------------------------


def _documents() -> list[Path]:
    return [
        *sorted(CONFIG_DIR.glob("*.json")),
        *sorted(CONFIG_DIR.glob("*.md")),
        *sorted((REPO_ROOT / "docs").glob("*.md")),
        REPO_ROOT / "README.md",
    ]


def _named_tools() -> dict[str, list[str]]:
    """Every tool name mentioned in a configuration or a document, by name."""
    found: dict[str, list[str]] = {}
    for path in _documents():
        for token in _TOOL_TOKEN.findall(path.read_text(encoding="utf-8")):
            found.setdefault(token, []).append(path.name)
    return found


def test_every_tool_named_anywhere_has_a_handler() -> None:
    router = ToolRouter(Doubles().services)
    unknown = {
        name: sorted(set(where))
        for name, where in _named_tools().items()
        if name not in TOOLS_BY_NAME
    }
    assert unknown == {}, "named but not in the catalogue"
    # The router's constructor raises when a published tool has no handler, so
    # constructing one already proves the mapping is total. What is left to
    # close is that every named tool is *accounted for* by this install: either
    # offered, or withheld with the capability reason that explains it. A tool
    # in neither list would be one a client could never reach and never be told
    # about.
    document = router.capability_document()
    accounted = set(document["published_tools"]) | set(document["withheld_tools"])
    assert set(_named_tools()) <= accounted


def test_the_documented_tools_are_the_whole_surface() -> None:
    """Nothing is published that no document mentions."""
    undocumented = sorted(set(TOOLS_BY_NAME) - set(_named_tools()))
    assert undocumented == []


def test_every_resource_uri_the_configuration_names_exists() -> None:
    text = (CONFIG_DIR / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"pz://[a-z/]+", text))
    assert named, "the README should name the resources a client can read"
    assert named <= set(RESOURCES_BY_URI)


def test_router_answers_a_tool_the_configuration_advertises() -> None:
    """One end-to-end call, so 'has a handler' is not only a construction check."""
    router = ToolRouter(Doubles().services)
    payload = router.call("pz_session_status", {})
    assert payload["ok"] is True
    assert payload["tool"] == "pz_session_status"


def test_the_configuration_readme_lists_the_whole_surface() -> None:
    """Per-document, not unioned across them — which is how this went stale.

    ``test_the_documented_tools_are_the_whole_surface`` asks whether a tool is
    named *anywhere*, so ``docs/MCP_TOOLS.md`` naming all of them satisfied it
    while ``configs/mcp/README.md`` still said "Nineteen tools" and listed the
    seven actions of an older build. A client reads that file to learn what it
    can call; a list that quietly stops covering half the surface is worse than
    no list, because it reads as complete.
    """
    text = (CONFIG_DIR / "README.md").read_text(encoding="utf-8")
    named = set(_TOOL_TOKEN.findall(text))
    missing = sorted(set(TOOLS_BY_NAME) - named)
    assert missing == [], "the configuration README does not name these published tools"
    invented = sorted(named - set(TOOLS_BY_NAME))
    assert invented == [], "the configuration README names tools this build does not publish"
