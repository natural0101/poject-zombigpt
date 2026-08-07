"""Submodule discovery for the PyInstaller specs, without importing anything.

PyInstaller's own :func:`~PyInstaller.utils.hooks.collect_submodules` finds a
package's submodules by importing each one. That is fine for a library whose
modules import cleanly, and fatal for one that does not.

The MCP SDK does not. ``mcp.cli`` needs ``typer``, which arrives only with the
``mcp[cli]`` extra, and when it is missing the module does not raise
:class:`ImportError` — it prints a message and calls :func:`sys.exit`. That is a
:class:`SystemExit`, which is not an :class:`Exception`, so PyInstaller's
``on_error`` handling cannot catch it and the isolated child process dies. The
build fails with ``No module named 'typer'`` while packaging a program that
never touches ``typer``, ``mcp.cli`` or a command line of the SDK's at all.

Reading the directory finds the same names and executes none of them, so a
submodule that cannot be imported in this environment is still packaged, and one
that must not be imported is never imported. The trade is that a module created
at runtime is invisible here — the SDK has none, and a name that is not a file
could not be packaged anyway.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Iterator
from pathlib import Path

__all__ = ["PackageNotFound", "submodules_on_disk"]

#: Directories that hold no importable module worth packaging.
_SKIP_DIRS = frozenset({"__pycache__", "tests", "test"})


class PackageNotFound(RuntimeError):
    """A package named for collection is not installed in the build environment.

    Raised rather than returning an empty list: an empty hidden-import list
    produces an executable that builds cleanly and fails on first use, which is
    the failure this whole exercise exists to stop shipping.
    """


def _locations(package: str) -> tuple[Path, ...]:
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:  # a parent that will not import
        raise PackageNotFound(f"{package}: cannot be located ({exc})") from exc
    if spec is None or not spec.submodule_search_locations:
        raise PackageNotFound(f"{package}: is not installed, or is not a package")
    return tuple(Path(item) for item in spec.submodule_search_locations)


def _walk(root: Path, prefix: str) -> Iterator[str]:
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_dir():
            if not (entry / "__init__.py").is_file():
                continue  # a data directory, not a package
            yield f"{prefix}.{entry.name}"
            yield from _walk(entry, f"{prefix}.{entry.name}")
        elif entry.suffix == ".py" and entry.stem != "__init__":
            yield f"{prefix}.{entry.stem}"
        elif entry.suffix in {".so", ".pyd"}:
            # An extension module: `foo.cpython-312-x86_64-linux-gnu.so`.
            yield f"{prefix}.{entry.name.split('.', 1)[0]}"


def submodules_on_disk(package: str, *, exclude: Iterable[str] = ()) -> list[str]:
    """*package* and every submodule under it, as dotted names.

    Args:
        package: the top-level package to collect.
        exclude: dotted prefixes to leave out. A prefix excludes the module
            itself and everything under it, so ``mcp.cli`` also drops
            ``mcp.cli.claude``.

    Raises:
        PackageNotFound: when *package* is not importable here.
    """
    blocked = tuple(exclude)
    found = {package}
    for location in _locations(package):
        found.update(_walk(location, package))
    return sorted(
        name
        for name in found
        if not any(name == item or name.startswith(f"{item}.") for item in blocked)
    )
