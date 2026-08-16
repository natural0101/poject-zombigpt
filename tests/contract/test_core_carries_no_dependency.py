"""``pz_agent_core`` carries no third-party runtime dependency, and imports downward.

Two lines of the repository map, neither of which had a check. The first is the
strongest architectural claim in this repository — *zero third-party runtime
dependencies, no MCP SDK, no LLM SDK, no UI* — and it is what lets the domain
layer be embedded, tested and reasoned about without an environment. The second
is the layering: core sits at the bottom, so ``pz_agent_voice`` and the two
packages above it are things core must never name.

"Zero" is not literally true, and the difference matters enough to be encoded
rather than described. ``knowledge/loader.py`` names ``yaml`` — inside a
function, inside a ``try``, whose ``ImportError`` handler raises the typed
``CorpusError(YAML_UNAVAILABLE)`` that stops planning rather than quietly
continuing without the rules the user configured. That is an *optional* parser,
honestly refused when absent, and core still runs with nothing installed. So the
rule this file enforces is the one that is actually true: a third-party name may
appear only behind a guard that turns its absence into a refusal, and only when
it is listed here with the reason.

Measured, not assumed, and the measurement is why the check is static. Importing
all 109 core modules in one process pulls in nothing outside the standard
library — and would have reported "zero" while missing ``yaml`` entirely,
because a deferred import inside a function does not run at import time. That is
the same shape as ``pz_agent_mcp/__main__.py``'s deliberate deferred import of
the CLI. A runtime probe cannot see either.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
CORE_SRC: Final = REPO_ROOT / "packages" / "pz_agent_core" / "src"
CORE_PACKAGE: Final = "pz_agent_core"

#: Packages that sit above core. Naming any of them would invert the layering
#: the repository map describes and make the domain layer unusable without the
#: things built on top of it.
SIBLING_PACKAGES: Final = frozenset({"pz_agent_cli", "pz_agent_mcp", "pz_agent_voice"})

#: Third-party names core may reach, each with why it is not a dependency.
#: An entry here is a promise that the import is guarded and its absence is a
#: typed refusal — which :func:`test_every_allowed_import_is_a_refusal_when_absent`
#: checks rather than trusts.
ALLOWED_THIRD_PARTY: Final = {
    "yaml": (
        "the knowledge corpus parser, reached only through "
        "knowledge.loader._yaml_module, which turns ImportError into "
        "CorpusError(YAML_UNAVAILABLE) and stops planning"
    ),
}


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE_SRC.rglob("*.py") if p.is_file())


def _imported_names(tree: ast.AST) -> list[tuple[ast.stmt, str]]:
    """Every absolute top-level name this tree imports, node included.

    Relative imports are skipped: by construction they stay inside core. The
    node comes back with the name so a caller can ask *where* the import sits,
    which is the whole question for a guarded one.
    """
    found: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append((node, node.module.split(".")[0]))
    return found


def _foreign(name: str) -> bool:
    """True for a name that is neither the standard library nor core itself.

    ``sys.stdlib_module_names`` rather than a list written here: a hand-kept
    copy of the standard library is a list that drifts, and this one is the
    interpreter's own answer for the interpreter running the test.
    """
    return name not in sys.stdlib_module_names and name != CORE_PACKAGE


def _guarded_import_lines(tree: ast.AST) -> set[int]:
    """Lines of imports that sit inside a ``try`` catching ``ImportError``.

    The handler is what makes an optional parser optional: without it the name
    is a dependency wearing a deferred import as a disguise.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import_error = any(_names_import_error(handler.type) for handler in node.handlers)
        if not catches_import_error:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    guarded.add(child.lineno)
    return guarded


def _names_import_error(node: ast.expr | None) -> bool:
    if node is None:  # a bare `except:`; check_forbidden.py refuses those anyway
        return False
    candidates = node.elts if isinstance(node, ast.Tuple) else [node]
    return any(
        isinstance(candidate, ast.Name) and candidate.id in {"ImportError", "ModuleNotFoundError"}
        for candidate in candidates
    )


def test_core_names_no_third_party_module_it_has_not_declared() -> None:
    undeclared: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, name in _imported_names(tree):
            if not _foreign(name) or name in ALLOWED_THIRD_PARTY:
                continue
            undeclared.append(f"{path.relative_to(CORE_SRC)}:{node.lineno} imports {name}")

    assert not undeclared, (
        "pz_agent_core must carry no third-party runtime dependency:\n"
        + "\n".join(f"  {row}" for row in undeclared)
        + "\nIf the import is optional, guard it so ImportError becomes a typed "
        "refusal and add it to ALLOWED_THIRD_PARTY with that reason. If it is "
        "not optional, it does not belong in the domain layer."
    )


def test_every_allowed_import_is_a_refusal_when_absent() -> None:
    """The allowance is a promise about the guard, so the guard is checked.

    Listing a name here without this would turn the allowance into a way to
    smuggle a hard dependency in: the entry would say "optional" and the code
    would raise ``ImportError`` at the user.
    """
    unguarded: list[str] = []
    seen: set[str] = set()
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = _guarded_import_lines(tree)
        for node, name in _imported_names(tree):
            if name not in ALLOWED_THIRD_PARTY:
                continue
            seen.add(name)
            if node.lineno not in guarded:
                unguarded.append(f"{path.relative_to(CORE_SRC)}:{node.lineno} imports {name}")

    assert not unguarded, (
        "an allowed third-party import is not behind a try/except ImportError, so "
        "its absence is a crash rather than a refusal:\n"
        + "\n".join(f"  {row}" for row in unguarded)
    )
    assert seen == set(ALLOWED_THIRD_PARTY), (
        f"ALLOWED_THIRD_PARTY lists {sorted(set(ALLOWED_THIRD_PARTY) - seen)} that core no "
        "longer imports; an allowance nobody uses is one nobody notices being used again"
    )


def test_core_imports_downward_only() -> None:
    """Core is the bottom layer; naming a package above it inverts that."""
    upward: list[str] = []
    for path in _core_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, name in _imported_names(tree):
            if name in SIBLING_PACKAGES:
                upward.append(f"{path.relative_to(CORE_SRC)}:{node.lineno} imports {name}")

    assert not upward, "pz_agent_core must not import a package built on it:\n" + "\n".join(
        f"  {row}" for row in upward
    )


def test_the_scan_sees_a_deferred_import(tmp_path: Path) -> None:
    """The control: without it these tests could be asserting over nothing.

    An import inside a function is the shape that matters — it is how the one
    real third-party name in core is written, and how ``pz_agent_mcp`` reaches
    the CLI — and a scan that walked only module-level statements would report
    a clean tree while missing every one of them.
    """
    source = tmp_path / "sample.py"
    source.write_text(
        "def load():\n    import httpx\n    return httpx\n",
        encoding="utf-8",
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    names = [name for _, name in _imported_names(tree)]

    assert names == ["httpx"]
    assert _foreign("httpx")
    assert not _foreign("json")
    assert _guarded_import_lines(tree) == set()
