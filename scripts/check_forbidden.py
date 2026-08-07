#!/usr/bin/env python3
"""Static gate against the shortcuts this project explicitly forbids.

Three families of finding, all of them from the master prompt's "запрещённые
сокращения" list:

1. **Stubs on the critical path** — ``TODO``, bare ``pass`` as a whole function
   body, ``NotImplementedError``, ``...`` bodies in shipped modules.
2. **Dangerous primitives** — ``eval``, ``exec``, ``os.system``, ``subprocess``
   with ``shell=True``, ``pickle.loads``. The LLM boundary must not be able to
   reach any of these, so they are banned outright in the shipped packages.
3. **Secrets** — anything that looks like a committed API key.

Run standalone (``python scripts/check_forbidden.py``) or via ``scripts/check.sh``.
Exit code is 1 when anything is found.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Shipped source. Tests and scripts are held to a looser standard: a test may
#: legitimately assert that NotImplementedError is raised, and this very file
#: has to name the forbidden functions to look for them.
SHIPPED_ROOTS = (
    REPO_ROOT / "packages",
    REPO_ROOT / "pz-mod",
)

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
}

BANNED_CALLS = {
    "eval": "arbitrary code execution",
    "exec": "arbitrary code execution",
    "compile": "arbitrary code execution",
}

BANNED_ATTR_CALLS = {
    ("os", "system"): "shell execution",
    ("os", "popen"): "shell execution",
    ("pickle", "loads"): "unsafe deserialisation",
    ("pickle", "load"): "unsafe deserialisation",
    ("subprocess", "getoutput"): "shell execution",
}

#: The anti-cheat floor, as names rather than as a promise. The agent plays the
#: game the way a mod is allowed to: through the modding API and the file IPC.
#: Nothing in shipped code may open another process's memory, write into it, or
#: start a thread there — the primitives a cheat or an injector is made of.
#: Banned as identifiers wherever they appear, because every route to them
#: (ctypes, pywin32, a vendored wrapper) ends in one of these names.
BANNED_PROCESS_TAMPERING = {
    "WriteProcessMemory": "writing another process's memory",
    "ReadProcessMemory": "reading another process's memory",
    "CreateRemoteThread": "code injection",
    "VirtualAllocEx": "allocating in another process",
    "VirtualProtectEx": "changing another process's page protection",
    "NtWriteVirtualMemory": "writing another process's memory",
    "process_vm_writev": "writing another process's memory",
    "PTRACE_POKEDATA": "writing another process's memory",
}

#: ``ctypes`` reaches the OS with no seatbelt, so importing it is confined to
#: the one audited use: the descriptor's liveness probe, which opens a process
#: handle with PROCESS_QUERY_LIMITED_INFORMATION — enough to learn that a pid
#: exists, deliberately not enough to do anything to it.
CTYPES_ALLOWED_IN = {
    Path("packages/pz_agent_core/src/pz_agent_core/rpc/descriptor.py"),
}

SECRET_PATTERNS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "Anthropic API key"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "OpenAI-style API key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub personal access token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
)

#: ``TODO`` in prose is fine; ``TODO`` in shipped code is a stub marker.
STUB_MARKER = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

LUA_STUB = re.compile(r"--\s*(TODO|FIXME|XXX)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.line}: [{self.rule}] {self.detail}"


def _iter_files(roots: tuple[Path, ...], suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            found.append(path)
    return found


def _is_trivial_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return a reason when a function body is an empty stub, else None.

    Protocol classes legitimately have ``...`` bodies, so a body of ``...`` is
    only reported outside a ``Protocol``/``ABC`` context — which the caller
    signals by not passing those functions in.
    """
    body = [
        stmt
        for stmt in node.body
        if not isinstance(stmt, ast.Expr)
        or not isinstance(stmt.value, ast.Constant)
        or not isinstance(stmt.value.value, str)
    ]
    if not body:
        return "function body is only a docstring"
    if len(body) == 1:
        only = body[0]
        if isinstance(only, ast.Pass):
            return "function body is a bare `pass`"
        if (
            isinstance(only, ast.Expr)
            and isinstance(only.value, ast.Constant)
            and only.value.value is Ellipsis
        ):
            return "function body is `...`"
        if isinstance(only, ast.Raise):
            exc = only.exc
            name = None
            if isinstance(exc, ast.Name):
                name = exc.id
            elif isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            if name == "NotImplementedError":
                return "function raises NotImplementedError"
    return None


def _protocol_or_abstract(cls: ast.ClassDef) -> bool:
    """True for Protocol/ABC classes, whose empty bodies are the point."""
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in {"Protocol", "ABC"}:
            return True
        if isinstance(base, ast.Attribute) and base.attr in {"Protocol", "ABC"}:
            return True
    return any(kw.arg == "metaclass" for kw in cls.keywords)


def _abstract_decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in {"abstractmethod", "abstractproperty", "overload"}:
            return True
    return False


def check_python(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8")

    for lineno, line in enumerate(text.splitlines(), start=1):
        match = STUB_MARKER.search(line)
        if match:
            findings.append(
                Finding(path, lineno, "stub-marker", f"{match.group(1)} marker in shipped code")
            )

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [*findings, Finding(path, exc.lineno or 0, "syntax", str(exc.msg))]

    # Functions inside a Protocol/ABC are allowed to be empty.
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _protocol_or_abstract(node):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    exempt.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if id(node) in exempt or _abstract_decorated(node):
                continue
            reason = _is_trivial_body(node)
            if reason is not None:
                findings.append(Finding(path, node.lineno, "stub-body", f"{node.name}: {reason}"))

        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(
                Finding(
                    path,
                    node.lineno,
                    "bare-except",
                    "`except:` with no exception type also catches KeyboardInterrupt and "
                    "SystemExit; name the exceptions this handler is for",
                )
            )

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                detail = f"{func.id}(): {BANNED_CALLS[func.id]}"
                findings.append(Finding(path, node.lineno, "banned-call", detail))
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in BANNED_ATTR_CALLS:
                    findings.append(
                        Finding(
                            path,
                            node.lineno,
                            "banned-call",
                            f"{key[0]}.{key[1]}(): {BANNED_ATTR_CALLS[key]}",
                        )
                    )
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    findings.append(Finding(path, node.lineno, "banned-call", "shell=True"))

        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in BANNED_PROCESS_TAMPERING:
                findings.append(
                    Finding(
                        path,
                        node.lineno,
                        "process-tampering",
                        f"{name}: {BANNED_PROCESS_TAMPERING[name]}; the agent plays "
                        "through the modding API, never through another process's memory",
                    )
                )

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(m == "ctypes" or m.startswith("ctypes.") for m in modules):
                relative = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
                if relative not in CTYPES_ALLOWED_IN:
                    findings.append(
                        Finding(
                            path,
                            node.lineno,
                            "ctypes-outside-the-probe",
                            "ctypes is confined to the descriptor's liveness probe; "
                            "a new use needs its own audit and an entry in "
                            "CTYPES_ALLOWED_IN",
                        )
                    )
    return findings


def check_lua(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = LUA_STUB.search(line)
        if match:
            findings.append(
                Finding(path, lineno, "stub-marker", f"{match.group(1)} marker in shipped Lua")
            )
        if "loadstring" in line or ("load(" in line and "payload" in line):
            findings.append(
                Finding(path, lineno, "banned-call", "dynamic Lua loading is forbidden")
            )
    return findings


def check_secrets() -> list[Finding]:
    """Scan every tracked text file, not just shipped source."""
    findings: list[Finding] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in {".png", ".jpg", ".gif", ".zip", ".ico", ".pyc"}:
            continue
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(path, lineno, "secret", label))
    return findings


def main() -> int:
    findings: list[Finding] = []
    for path in _iter_files(SHIPPED_ROOTS, (".py",)):
        findings.extend(check_python(path))
    for path in _iter_files(SHIPPED_ROOTS, (".lua",)):
        findings.extend(check_lua(path))
    findings.extend(check_secrets())

    if findings:
        print(f"{len(findings)} forbidden pattern(s) found:")
        for finding in findings:
            print(f"  {finding.render()}")
        return 1
    print("No forbidden patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
