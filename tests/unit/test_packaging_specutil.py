"""Submodule collection for the Windows specs, driven by a synthetic package.

This is the code that decides what goes into `pz-agent-mcp.exe`, and it has two
opposite ways to be wrong. Collect too little and the executable builds cleanly
and fails on its first tool call, which is the shape of failure this project
keeps finding. Collect by importing and the build dies on a module the program
never runs — which is what happened: `mcp.cli` calls `sys.exit` at import time
when its optional `typer` extra is absent, and a `SystemExit` is not an
`Exception`, so PyInstaller's `on_error` could not catch it.

The package tree here is built on disk rather than mocked, and one of its
modules raises `SystemExit` on import, so the "never imports anything" claim has
something that would fail if it stopped being true.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

SPEC_DIR: Final = Path(__file__).resolve().parents[2] / "packaging" / "windows"
sys.path.insert(0, str(SPEC_DIR))

from specutil import PackageNotFound, submodules_on_disk  # noqa: E402

#: A package shaped like the SDK: a subpackage that cannot be imported, a nested
#: one under it, an ordinary module, a data directory that is not a package, and
#: a `__pycache__` that must not be mistaken for one.
TREE: Final = {
    "__init__.py": "",
    "types.py": "",
    "server/__init__.py": "",
    "server/stdio.py": "",
    "server/lowlevel/__init__.py": "",
    "cli/__init__.py": "from .cli import app\n",
    # The exact shape that killed the build: not an ImportError.
    "cli/cli.py": "import sys\n\nprint('typer is required')\nsys.exit(1)\n",
    "shared/__init__.py": "",
    "py.typed": "",
    "data/schema.json": "{}",
    "__pycache__/types.cpython-312.pyc": "",
}


@pytest.fixture
def sdk(tmp_path: Path) -> Iterator[str]:
    """A package named `fake_sdk`, importable only through *tmp_path*."""
    root = tmp_path / "fake_sdk"
    for relative, body in TREE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        yield "fake_sdk"
    finally:
        sys.path.remove(str(tmp_path))
        for name in [item for item in sys.modules if item.startswith("fake_sdk")]:
            del sys.modules[name]


def test_every_module_in_the_package_is_collected(sdk: str) -> None:
    collected = submodules_on_disk(sdk)

    assert collected == [
        "fake_sdk",
        "fake_sdk.cli",
        "fake_sdk.cli.cli",
        "fake_sdk.server",
        "fake_sdk.server.lowlevel",
        "fake_sdk.server.stdio",
        "fake_sdk.shared",
        "fake_sdk.types",
    ]


def test_collecting_imports_nothing(sdk: str) -> None:
    """The whole point: `fake_sdk.cli.cli` calls `sys.exit(1)` when imported.

    An importing collector dies here — not with an ImportError it could be told
    to ignore, but with a `SystemExit` that no `except Exception` catches.
    """
    submodules_on_disk(sdk)

    assert not [name for name in sys.modules if name.startswith(sdk)], (
        "collection imported the package it was only supposed to read"
    )


def test_an_excluded_prefix_takes_its_children_with_it(sdk: str) -> None:
    """`mcp.cli` has to drop `mcp.cli.cli`, or the exclusion achieves nothing."""
    collected = submodules_on_disk(sdk, exclude=[f"{sdk}.cli"])

    assert f"{sdk}.cli" not in collected
    assert f"{sdk}.cli.cli" not in collected
    assert f"{sdk}.server.stdio" in collected, "the exclusion took an unrelated module"


def test_an_exclusion_does_not_match_a_name_that_merely_starts_the_same(sdk: str) -> None:
    """Prefix matching is on dotted components, not on characters.

    Excluding `fake_sdk.cli` must not also drop a sibling called
    `fake_sdk.client`; that is the bug a `startswith` on the bare string gives.
    """
    (Path(sys.path[0]) / sdk / "client.py").write_text("", encoding="utf-8")

    collected = submodules_on_disk(sdk, exclude=[f"{sdk}.cli"])

    assert f"{sdk}.client" in collected


def test_a_data_directory_is_not_mistaken_for_a_package(sdk: str) -> None:
    """`data/` has no `__init__.py`, so there is no `fake_sdk.data` to import."""
    collected = submodules_on_disk(sdk)

    assert f"{sdk}.data" not in collected
    assert f"{sdk}.__pycache__" not in collected
    assert not any(name.endswith(".py_typed") or name.endswith(".py") for name in collected)


def test_a_package_that_is_not_installed_is_a_refusal_not_an_empty_list() -> None:
    """An empty hidden-import list builds an executable that fails on first use.

    Which is worse than a failed build, because the failure arrives after the
    release rather than during it.
    """
    with pytest.raises(PackageNotFound):
        submodules_on_disk("a_package_that_is_not_installed_anywhere")


def test_a_module_is_not_a_package(tmp_path: Path) -> None:
    """`find_spec` succeeds for a plain module; there is nothing to walk."""
    (tmp_path / "lonely.py").write_text("", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(PackageNotFound):
            submodules_on_disk("lonely")
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("lonely", None)
