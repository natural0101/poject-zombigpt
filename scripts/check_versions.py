#!/usr/bin/env python3
"""Release gate: the five versions must agree everywhere they are restated.

``pz_agent_core.version`` is the source of truth. This script fails when any of
the following disagrees with it:

* ``pyproject.toml`` → ``project.version`` vs ``PRODUCT_VERSION``
* ``pz-mod/42/mod.info`` → ``modversion`` vs ``MOD_VERSION``
* ``schemas/*.schema.json`` → ``schema_version``/``protocol_version`` consts
* ``CHANGELOG.md`` → newest released heading vs ``PRODUCT_VERSION``
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "pz_agent_core" / "src"))

from pz_agent_core.version import (  # noqa: E402  (path set up above)
    MOD_VERSION,
    PRODUCT_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
)


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def check_pyproject(problems: list[str]) -> None:
    path = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    declared = data["project"]["version"]
    if declared != PRODUCT_VERSION:
        _fail(
            problems,
            f"pyproject.toml project.version={declared!r} != PRODUCT_VERSION={PRODUCT_VERSION!r}",
        )


def check_mod_info(problems: list[str]) -> None:
    path = REPO_ROOT / "pz-mod" / "42" / "mod.info"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^modversion=(.+)$", text, re.MULTILINE)
    if match is None:
        _fail(problems, "pz-mod/42/mod.info has no modversion= line")
        return
    declared = match.group(1).strip()
    if declared != MOD_VERSION:
        _fail(problems, f"mod.info modversion={declared!r} != MOD_VERSION={MOD_VERSION!r}")


def _const_of(node: object, key: str) -> str | None:
    """Read a ``{"const": ...}`` value for *key* out of a schema properties map."""
    if not isinstance(node, dict):
        return None
    prop = node.get("properties", {})
    if not isinstance(prop, dict):
        return None
    entry = prop.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("const"), str):
        return entry["const"]
    return None


def check_schemas(problems: list[str]) -> None:
    for path in sorted((REPO_ROOT / "schemas").glob("*.schema.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        schema_const = _const_of(data, "schema_version")
        if schema_const is not None and schema_const != SCHEMA_VERSION:
            _fail(
                problems,
                f"{path.name} schema_version const={schema_const!r} != {SCHEMA_VERSION!r}",
            )
        protocol_const = _const_of(data, "protocol_version")
        if protocol_const is not None and protocol_const != PROTOCOL_VERSION:
            _fail(
                problems,
                f"{path.name} protocol_version const={protocol_const!r} != {PROTOCOL_VERSION!r}",
            )


def check_changelog(problems: list[str]) -> None:
    path = REPO_ROOT / "CHANGELOG.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    versions = re.findall(r"^##\s*\[?(\d+\.\d+\.\d+)\]?", text, re.MULTILINE)
    if not versions:
        # An Unreleased-only changelog is valid before the first tag.
        if "## [Unreleased]" not in text and "## Unreleased" not in text:
            _fail(problems, "CHANGELOG.md has neither a version heading nor an Unreleased section")
        return
    if versions[0] != PRODUCT_VERSION:
        _fail(
            problems,
            f"CHANGELOG.md newest version {versions[0]!r} != PRODUCT_VERSION={PRODUCT_VERSION!r}",
        )


def main() -> int:
    problems: list[str] = []
    check_pyproject(problems)
    check_mod_info(problems)
    check_schemas(problems)
    check_changelog(problems)

    if problems:
        print(f"{len(problems)} version mismatch(es):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        "Versions in sync: "
        f"product={PRODUCT_VERSION} protocol={PROTOCOL_VERSION} "
        f"schema={SCHEMA_VERSION} mod={MOD_VERSION}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
